from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from pydantic import TypeAdapter

from carlina.runtime_composition import resolve_runtime_composition
from iarf_assistant.addons.translator import api_client
from iarf_assistant.addons.translator import models, review_coverage
from iarf_assistant.core import mcp_gateway
from iarf_assistant.addons.translator.mcp_settings import SentenceEdit
from iarf_assistant.extensions import db


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-review-mcp.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-review-mcp-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


@pytest.fixture
def client(monkeypatch):
    review_client = api_client.TranslatorClient(
        SimpleNamespace(), {"current_user_id": 41}
    )
    project = SimpleNamespace(
        id="project-1",
        user_id=41,
        engine_contract="document-block-v1",
    )
    monkeypatch.setattr(review_client, "_project", MagicMock(return_value=project))
    engine_gate = MagicMock()
    monkeypatch.setattr(api_client, "require_document_block_engine", engine_gate)
    return review_client, project, engine_gate


def test_review_methods_forward_context_and_proposal_flags(client, monkeypatch):
    review_client, project, engine_gate = client
    outline = MagicMock(return_value={"outline": True})
    fetch = MagicMock(return_value={"fetch": True})
    search = MagicMock(return_value={"search": True})
    source_pages = MagicMock(return_value={"pages": True})
    qa = MagicMock(return_value={"qa": True})
    explain = MagicMock(return_value={"explain": True})
    monkeypatch.setattr(api_client.document_review, "review_outline", outline)
    monkeypatch.setattr(api_client.document_review, "review_fetch", fetch)
    monkeypatch.setattr(api_client.document_review, "review_search", search)
    monkeypatch.setattr(api_client.document_review, "review_source_pages", source_pages)
    monkeypatch.setattr(api_client.qa_diagnostics, "qa_diagnostics", qa)
    monkeypatch.setattr(
        api_client.document_review, "explanation_evidence", explain
    )

    assert review_client.review_outline("project-1", cursor="next", limit=7) == {
        "outline": True
    }
    assert review_client.review_fetch(
        "project-1",
        block_ids=["block-2"],
        cursor="fetch-next",
        limit=3,
        include_research=True,
        include_proposals=True,
    ) == {"fetch": True}
    assert review_client.review_search(
        "project-1",
        "needle",
        fields=["target"],
        roles=["paragraph"],
        before=2,
        after=4,
        cursor="search-next",
        limit=5,
        include_proposals=True,
    ) == {"search": True}
    assert review_client.review_search("project-1", "default-fields") == {
        "search": True
    }
    assert review_client.review_source_pages("project-1", ["block-2"]) == {
        "pages": True
    }
    assert review_client.qa_diagnostics(
        "project-1",
        block_ids=["block-2"],
        categories=["references"],
        severities=["integrity"],
        limit=9,
        include_spelling=False,
    ) == {"qa": True}
    assert review_client.review_explain(
        "project-1", ["block-2"], "Why this wording?"
    ) == {"explain": True}

    assert engine_gate.call_count == 7
    outline.assert_called_once_with(project, cursor="next", limit=7)
    fetch.assert_called_once_with(
        project,
        block_ids=["block-2"],
        order_start=None,
        order_end=None,
        cursor="fetch-next",
        limit=3,
        include_research=True,
        include_proposals=True,
        include_alignment=False,
    )
    assert search.call_args_list[0].args == (project,)
    assert search.call_args_list[0].kwargs == {
        "query": "needle",
        "fields": ["target"],
        "roles": ["paragraph"],
        "before": 2,
        "after": 4,
        "cursor": "search-next",
        "limit": 5,
        "include_proposals": True,
        "mode": "literal",
        "regex_flags": (),
    }
    assert search.call_args_list[1].kwargs["fields"] == ("source", "target")
    assert search.call_args_list[1].kwargs["roles"] == ()
    source_pages.assert_called_once_with(project, block_ids=["block-2"])
    qa.assert_called_once_with(
        project,
        block_ids=["block-2"],
        categories=["references"],
        severities=["integrity"],
        cursor=None,
        limit=9,
        include_spelling=False,
        group_by="finding",
        include_excerpts=True,
        group_id=None,
    )
    explain.assert_called_once_with(
        project, block_ids=["block-2"], question="Why this wording?"
    )


def test_review_methods_fail_before_service_when_engine_is_not_document_block(
    client, monkeypatch
):
    review_client, _project, engine_gate = client
    engine_gate.side_effect = ValueError("engine-contract-required")
    review = MagicMock()
    monkeypatch.setattr(api_client.document_review, "review_outline", review)

    with pytest.raises(ValueError, match="engine-contract-required"):
        review_client.review_outline("project-1")

    review.assert_not_called()


@pytest.mark.parametrize("restore_parent_alignment", [False, True])
def test_target_update_forwards_owner_scope_and_returns_safe_block(
    client, monkeypatch, restore_parent_alignment
):
    review_client, project, engine_gate = client
    commit = MagicMock()
    monkeypatch.setattr(db.session, "commit", commit)
    block = {"id": "block-1", "target": {"active_version": 1}}
    persist = MagicMock(return_value=block)
    monkeypatch.setattr(api_client, "persist_manual_candidate", persist)

    result = review_client.target_update(
        "project-1",
        "block-1",
        {"candidate": True},
        0,
        "target-edit-1",
        restore_parent_alignment=restore_parent_alignment,
    )

    assert result == {"success": True, "block": block}
    engine_gate.assert_called_once_with(project)
    persist.assert_called_once_with(
        project,
        "block-1",
        {"candidate": True},
        0,
        author_user_id=41,
        idempotency_key="target-edit-1",
        restore_parent_alignment=restore_parent_alignment,
    )
    commit.assert_called_once_with()


def test_compact_target_retry_returns_receipt_revision_without_prose(
    app, client, monkeypatch
):
    review_client, project, _engine_gate = client
    block = {
        "id": "block-1",
        "target": {
            "active_version": 4,
            "editor": {"content": "Private corrected prose"},
            "alignment": {"review_state": "reviewed", "groups": [{}, {}]},
        },
        "review": {
            "decision": "approved", "effective_decision": "pending", "stale": True,
            "current_target_revision_id": "later-current-revision",
            "coverage": {"bilingual": {"source_anchor_ids": ["anchor-1"]}},
        },
        "reader_notes": [{"state": "needs_review"}, {"state": "active"}],
    }
    monkeypatch.setattr(api_client, "persist_manual_candidate", MagicMock(return_value=block))
    monkeypatch.setattr(db.session, "commit", MagicMock())
    receipt_query = MagicMock()
    receipt_query.filter_by.return_value.one.return_value = SimpleNamespace(
        target_revision_id="original-edit-revision"
    )
    monkeypatch.setattr(models.TranslationTargetEditIdempotency, "query", receipt_query)

    result = review_client.target_update(
        "project-1", "block-1", {"candidate": True}, 3, " edit-key ",
        response_mode="compact",
    )

    assert result["target_revision_id"] == "original-edit-revision"
    assert result["target_version"] == 4
    assert result["alignment"] == {"review_state": "reviewed", "group_count": 2}
    assert result["reader_notes_needing_review"] == 1
    assert len(result["editor_sha256"]) == 64
    assert "block" not in result and "coverage" not in result["review"]
    assert "Private corrected prose" not in str(result)
    receipt_query.filter_by.assert_called_once_with(
        project_id=project.id, actor_type="owner", actor_id="user:41", idempotency_key="edit-key",
    )


def test_invalid_target_response_mode_fails_before_edit(client, monkeypatch):
    review_client, _project, _engine_gate = client
    persist = MagicMock()
    monkeypatch.setattr(api_client, "persist_manual_candidate", persist)
    monkeypatch.setattr(db.session, "rollback", MagicMock())
    with pytest.raises(ValueError, match="response_mode"):
        review_client.target_update("project-1", "block-1", {}, 0, "key", response_mode="brief")
    persist.assert_not_called()


@pytest.mark.parametrize("include_anchor_ids", [False, True])
def test_coverage_read_pages_blocks_and_keeps_scan_summary(
    client, monkeypatch, include_anchor_ids
):
    review_client, _project, _engine_gate = client
    manifest = {
        "blocks": [
            {
                "block_id": f"block-{index}",
                "coverage": [{"source_anchor_ids": [f"anchor-{index}"]}],
                "kinds": {"bilingual": {
                    "reviewed_source_anchor_ids": [f"anchor-{index}"],
                    "reviewed_source_anchor_count": 1,
                }},
            }
            for index in range(3)
        ],
        "summary": {"bilingual": {"complete": 3, "partial": 0, "missing": 0}},
    }
    monkeypatch.setattr(review_coverage, "review_coverage_manifest", MagicMock(return_value=manifest))
    result = review_client.review_coverage_get(
        "project-1", offset=1, limit=1, include_anchor_ids=include_anchor_ids,
    )
    assert result["pagination"] == {"offset": 1, "limit": 1, "total": 3, "has_more": True}
    assert result["summary"]["bilingual"]["complete"] == 3
    assert result["blocks"][0]["block_id"] == "block-1"
    coverage = result["blocks"][0]["coverage"][0]
    assert ("source_anchor_ids" in coverage) is include_anchor_ids
    assert ("reviewed_source_anchor_ids" in result["blocks"][0]["kinds"]["bilingual"]) is include_anchor_ids
    if not include_anchor_ids:
        assert coverage["source_anchor_count"] == 1


def test_mutations_use_owner_actor_project_and_safe_wire_projections(
    app, client, monkeypatch
):
    review_client, project, _engine_gate = client
    commit = MagicMock()
    rollback = MagicMock()
    monkeypatch.setattr(db.session, "commit", commit)
    monkeypatch.setattr(db.session, "rollback", rollback)

    raw_note = {
        "id": "note-1",
        "version": 2,
        "state": "active",
        "body": "Visible note",
        "citations": ["dossier-note-v1:private", "source-1"],
        "anchor_kind": "block",
        "author_type": "owner",
        "author_user_id": 99,
        "witness_before": "private",
        "record_hash": "private",
    }
    raw_dossier = {
        "id": "dossier-1",
        "revision_id": "revision-1",
        "version": 1,
        "scope": {"kind": "document"},
        "provenance": {
            "method": "external",
            "owner_user_id": 99,
            "request_fingerprint": "private",
        },
        "payload": {
            "evidence": "Visible",
            "record_hash": "private",
            "selection_fingerprint": "private",
        },
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
    }
    create = MagicMock(return_value=raw_note)
    revise = MagicMock(return_value=raw_note)
    transition = MagicMock(return_value=raw_note)
    submit = MagicMock(return_value=raw_dossier)
    monkeypatch.setattr(api_client.scholarly_review, "create_reader_note", create)
    monkeypatch.setattr(api_client.scholarly_review, "revise_reader_note", revise)
    monkeypatch.setattr(
        api_client.scholarly_review, "transition_reader_note", transition
    )
    monkeypatch.setattr(
        api_client.scholarly_review, "submit_research_dossier", submit
    )

    note = review_client.reader_note_create(
        "project-1",
        "block-1",
        "Visible note",
        {"kind": "block"},
        "note-create-1",
        citation_ids=["source-1"],
    )
    revised = review_client.reader_note_revise(
        "project-1",
        "note-1",
        "Revised",
        2,
        "note-revise-1",
        anchor={"kind": "block"},
        citation_ids=["source-1"],
        state="active",
    )
    transitioned = review_client.reader_note_set_state(
        "project-1", "note-1", "needs_review", 3, "note-state-1"
    )
    dossier = review_client.research_dossier_submit(
        "project-1",
        "submission-1",
        {"evidence": "Visible"},
        {"method": "external"},
        {"kind": "document", "block_ids": []},
    )

    assert note["citation_ids"] == ["source-1"]
    assert revised["citation_ids"] == ["source-1"]
    assert transitioned["citation_ids"] == ["source-1"]
    assert "author_user_id" not in note
    assert "witness_before" not in note
    assert "record_hash" not in dossier["payload"]
    assert "owner_user_id" not in dossier["provenance"]
    assert "request_fingerprint" not in dossier["provenance"]
    assert "selection_fingerprint" not in dossier["payload"]
    assert dossier["payload"] == {"evidence": "Visible"}
    assert commit.call_count == 4
    rollback.assert_not_called()

    assert create.call_args.args[0] is project
    assert create.call_args.kwargs["actor"].role == "owner"
    assert create.call_args.kwargs["actor"].user_id == 41
    assert create.call_args.kwargs["idempotency_key"] == "note-create-1"
    assert revise.call_args.kwargs["expected_head_version"] == 2
    assert revise.call_args.kwargs["idempotency_key"] == "note-revise-1"
    assert transition.call_args.kwargs["next_state"] == "needs_review"
    assert transition.call_args.kwargs["idempotency_key"] == "note-state-1"
    assert submit.call_args.kwargs["actor"].role == "owner"


def test_mutation_rolls_back_service_failure_and_does_not_commit(
    client, monkeypatch
):
    review_client, _project, _engine_gate = client
    commit = MagicMock()
    rollback = MagicMock()
    monkeypatch.setattr(db.session, "commit", commit)
    monkeypatch.setattr(db.session, "rollback", rollback)
    monkeypatch.setattr(
        api_client.scholarly_review,
        "create_reader_note",
        MagicMock(side_effect=ValueError("private service detail")),
    )

    with pytest.raises(ValueError, match="private service detail"):
        review_client.reader_note_create(
            "project-1",
            "block-1",
            "note",
            {"kind": "block"},
            "note-create-failure",
        )

    commit.assert_not_called()
    rollback.assert_called_once()


class _Server:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, description):
        def decorate(function):
            self.tools[name] = (function, description)
            return function

        return decorate


def test_gateway_registers_typed_review_tools_with_requested_defaults():
    server = _Server()
    application = Flask(__name__)
    bind_content_policy(application)
    mcp_gateway._register_product_tools(
        server,
        application,
        composition=resolve_runtime_composition("workbench", environment={}),
    )

    expected = {
        "carlina_translator_review_outline",
        "carlina_translator_review_fetch",
        "carlina_translator_review_source_pages",
        "carlina_translator_qa_diagnostics",
        "carlina_translator_review_search",
        "carlina_translator_review_explain",
        "carlina_translator_review_coverage_record",
        "carlina_translator_review_coverage_get",
        "carlina_translator_target_update",
        "carlina_translator_reader_note_create",
        "carlina_translator_reader_note_revise",
        "carlina_translator_reader_note_set_state",
        "carlina_translator_research_dossier_submit",
    }
    assert expected <= set(server.tools)
    assert inspect.signature(
        server.tools["carlina_translator_review_outline"][0]
    ).parameters["limit"].default == 100
    fetch_parameters = inspect.signature(
        server.tools["carlina_translator_review_fetch"][0]
    ).parameters
    assert fetch_parameters["limit"].default == 50
    assert fetch_parameters["include_proposals"].default is False
    search_parameters = inspect.signature(
        server.tools["carlina_translator_review_search"][0]
    ).parameters
    assert search_parameters["fields"].default is None
    assert search_parameters["roles"].default is None
    assert search_parameters["before"].default == 0
    assert search_parameters["after"].default == 0
    assert search_parameters["limit"].default == 20
    assert search_parameters["include_proposals"].default is False
    assert search_parameters["mode"].default == "literal"
    assert search_parameters["regex_flags"].default is None
    qa_parameters = inspect.signature(
        server.tools["carlina_translator_qa_diagnostics"][0]
    ).parameters
    assert qa_parameters["limit"].default == 100
    assert qa_parameters["include_spelling"].default is True
    assert qa_parameters["group_by"].default == "token"
    assert qa_parameters["include_excerpts"].default is False
    assert fetch_parameters["include_alignment"].default is False
    page_parameters = inspect.signature(
        server.tools["carlina_translator_review_source_pages"][0]
    ).parameters
    assert page_parameters["max_edge"].default == 1600
    target_parameters = inspect.signature(
        server.tools["carlina_translator_target_update"][0]
    ).parameters
    assert target_parameters["restore_parent_alignment"].default is False
    assert target_parameters["candidate"].default is None
    assert target_parameters["sentence_edit"].default is None
    assert target_parameters["response_mode"].default == "compact"
    assert all(
        target_parameters[name].default is inspect.Parameter.empty
        for name in (
            "project_id",
            "block_id",
            "expected_active_version",
            "idempotency_key",
        )
    )
    for name in (
        "carlina_translator_reader_note_create",
        "carlina_translator_reader_note_revise",
        "carlina_translator_reader_note_set_state",
    ):
        assert (
            inspect.signature(server.tools[name][0])
            .parameters["idempotency_key"]
            .default
            is inspect.Parameter.empty
        )
    assert (
        "not hidden reasoning" in server.tools["carlina_translator_review_explain"][1]
    )


@pytest.mark.parametrize("restore_parent_alignment", [False, True])
def test_gateway_routes_target_update_through_authenticated_owner(
    monkeypatch, restore_parent_alignment
):
    server = _Server()
    application = Flask(__name__)
    bind_content_policy(application)
    principal = SimpleNamespace(owner_user_id=41)
    client = SimpleNamespace(target_update=MagicMock(return_value={"success": True}))

    async def invoke_in_app(_application, function):
        return function()

    principal_lookup = MagicMock(return_value=principal)
    translator_lookup = MagicMock(return_value=client)
    monkeypatch.setattr(mcp_gateway, "_principal_from_request", principal_lookup)
    monkeypatch.setattr(mcp_gateway, "_translator_client", translator_lookup)
    monkeypatch.setattr(mcp_gateway, "_in_app", invoke_in_app)
    mcp_gateway._register_product_tools(
        server,
        application,
        composition=resolve_runtime_composition("workbench", environment={}),
    )

    result = asyncio.run(
        server.tools["carlina_translator_target_update"][0](
            project_id="project-1",
            block_id="block-1",
            candidate={"candidate": True},
            expected_active_version=3,
            idempotency_key="target-edit-1",
            restore_parent_alignment=restore_parent_alignment,
        )
    )

    assert result == {"success": True}
    principal_lookup.assert_called_once_with(required_scope="mcp.translator")
    translator_lookup.assert_called_once_with(application, 41)
    client.target_update.assert_called_once_with(
        "project-1",
        "block-1",
        {"candidate": True},
        3,
        "target-edit-1",
        restore_parent_alignment=restore_parent_alignment,
        response_mode="compact",
    )


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (
            {
                "action": "replace",
                "sentence_order": 2,
                "text": "Zmienione 😀.",
            },
            {"action": "replace", "sentence_order": 2, "text": "Zmienione 😀."},
        ),
        (
            {
                "action": "split",
                "sentence_order": 1,
                "text": "😀 Raz. Dwa.",
                "offset": 7,
            },
            {
                "action": "split",
                "sentence_order": 1,
                "text": "😀 Raz. Dwa.",
                "offset": 7,
            },
        ),
        (
            {"action": "merge_next", "sentence_order": 3},
            {"action": "merge_next", "sentence_order": 3},
        ),
    ],
)
def test_gateway_forwards_typed_sentence_edit_with_named_json_fields(
    monkeypatch, edit, expected
):
    server = _Server()
    application = Flask(__name__)
    bind_content_policy(application)
    principal = SimpleNamespace(owner_user_id=41)
    client = SimpleNamespace(target_update=MagicMock(return_value={"success": True}))

    async def invoke_in_app(_application, function):
        return function()

    monkeypatch.setattr(mcp_gateway, "_principal_from_request", MagicMock(return_value=principal))
    monkeypatch.setattr(mcp_gateway, "_translator_client", MagicMock(return_value=client))
    monkeypatch.setattr(mcp_gateway, "_in_app", invoke_in_app)
    mcp_gateway._register_product_tools(
        server,
        application,
        composition=resolve_runtime_composition("workbench", environment={}),
    )

    typed_edit = TypeAdapter(SentenceEdit).validate_python(edit)
    result = asyncio.run(
        server.tools["carlina_translator_target_update"][0](
            project_id="project-1",
            block_id="block-1",
            expected_active_version=8,
            idempotency_key="sentence-edit-1",
            sentence_edit=typed_edit,
        )
    )

    assert result == {"success": True}
    client.target_update.assert_called_once_with(
        "project-1",
        "block-1",
        None,
        8,
        "sentence-edit-1",
        restore_parent_alignment=False,
        sentence_edit=expected,
        response_mode="compact",
    )
