from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor.mcp_service import DocProcessorMcpService
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.translator.models import TranslationProject
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_gateway import SERVER_INSTRUCTIONS
from iarf_assistant.core.mcp_jobs import (
    create_execution,
    get_or_create_work,
    issue_access_key,
    list_executions,
    resolve_access_key,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User

EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp_listing.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-listing-test-secret",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    db.create_all()
    owner = User(id=1, email="owner@example.test", password_hash="x")
    other = User(id=2, email="other@example.test", password_hash="x")
    db.session.add_all([owner, other])
    db.session.commit()
    try:
        yield application
    finally:
        db.session.remove()
        context.pop()


def test_server_instructions_guidance():
    assert "carlina_document_list" in SERVER_INSTRUCTIONS
    assert "carlina_translator_list" in SERVER_INSTRUCTIONS
    assert "carlina_job_list" in SERVER_INSTRUCTIONS
    assert "carlina_work_claim" in SERVER_INSTRUCTIONS
    assert "carlina_work_submit" in SERVER_INSTRUCTIONS
    assert "waiting_external" in SERVER_INSTRUCTIONS
    assert "waiting_review" in SERVER_INSTRUCTIONS
    assert "durable across sessions" in SERVER_INSTRUCTIONS
    assert "job_list defaults to nonterminal history" in SERVER_INSTRUCTIONS
    assert '"schema_version":"carlina.external-executor.v1"' in SERVER_INSTRUCTIONS
    assert '"input_modalities":["text","image"]' in SERVER_INSTRUCTIONS
    assert '"supports_structured_output":true' in SERVER_INSTRUCTIONS
    assert '"schema_version":"carlina.document-batches.v1"' in SERVER_INSTRUCTIONS
    assert '"scheduling_mode":"sequential"' in SERVER_INSTRUCTIONS
    assert '"batch_id":"pages-001-004"' in SERVER_INSTRUCTIONS
    assert "document_batch" in SERVER_INSTRUCTIONS
    assert "source.page_count" in SERVER_INSTRUCTIONS
    assert "singular modality names" in SERVER_INSTRUCTIONS


def test_doc_processor_list_documents(app: Flask):
    doc1 = ProcessedDocument(
        id=str(uuid.uuid4()),
        user_id=1,
        filename="completed_doc.pdf",
        file_path="/tmp/completed_doc.pdf",
        status="completed",
        output_html="<html><body><h1>Done</h1></body></html>",
        settings={"document_metadata": {"language": "en", "pages": 5}},
    )
    doc2 = ProcessedDocument(
        id=str(uuid.uuid4()),
        user_id=1,
        filename="review_doc.pdf",
        file_path="/tmp/review_doc.pdf",
        status="reviewing_images",
        settings={
            "document_metadata": {"language": "en", "pages": 10},
            "pdf_state": {"html_parts": ["<p>p1</p>", '<p><img src="fig.png"/></p>']},
        },
    )
    other_doc = ProcessedDocument(
        id=str(uuid.uuid4()),
        user_id=2,
        filename="other_user.pdf",
        file_path="/tmp/other_user.pdf",
        status="completed",
    )
    db.session.add_all([doc1, doc2, other_doc])
    db.session.commit()

    from unittest.mock import MagicMock

    service = DocProcessorMcpService(MagicMock(), owner_user_id=1)

    # 1. All documents for owner
    all_docs = service.list_documents()
    assert len(all_docs) == 2
    filenames = {d["filename"] for d in all_docs}
    assert filenames == {"completed_doc.pdf", "review_doc.pdf"}

    # 2. Filter by status="completed"
    completed = service.list_documents(status="completed")
    assert len(completed) == 1
    assert completed[0]["filename"] == "completed_doc.pdf"
    assert completed[0]["has_output"] is True

    # 3. Filter by status="reviewing_images" and verify review metadata
    reviewing = service.list_documents(status="reviewing_images")
    assert len(reviewing) == 1
    assert reviewing[0]["filename"] == "review_doc.pdf"
    assert reviewing[0]["review_kind"] == "images"
    assert reviewing[0]["review_details"]["figure_count"] == 1
    assert reviewing[0]["review_details"]["pages"] == [2]

    # 4. Limit parameter
    limited = service.list_documents(limit=1)
    assert len(limited) == 1


def test_list_executions_filtering_and_counts(app: Flask):
    issued = issue_access_key(
        1,
        "Test Key",
        ["mcp.doc_processor", "mcp.translator", "mcp.work"],
        ["/tmp"],
    )
    db.session.commit()
    principal = resolve_access_key(issued.raw_token)
    assert principal is not None

    # Create executions
    e1 = create_execution(
        1,
        "doc_processor",
        "document",
        "doc-uuid-1",
        request_json={"executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    e2 = create_execution(
        1,
        "translator",
        "project",
        "proj-uuid-1",
        request_json={"executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    e3 = create_execution(
        2,
        "doc_processor",
        "document",
        "doc-uuid-2",
        request_json={"executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    db.session.commit()

    # Add work item to e1
    get_or_create_work(
        e1.id,
        {
            "task_kind": "doc_processor_pdf_parser",
            "page_number": 1,
            "prompt": "Parse page 1",
        },
    )

    # 1. List executions for principal (user 1 only)
    executions = list_executions(principal)
    assert len(executions) == 2
    e_map = {e["id"]: e for e in executions}
    assert e1.id in e_map
    assert e2.id in e_map
    assert e3.id not in e_map

    # Check pending work count on e1
    assert e_map[e1.id]["pending_work_count"] == 1
    assert e_map[e2.id]["pending_work_count"] == 0

    # 2. Filter by addon_id="translator"
    trans_execs = list_executions(principal, addon_id="translator")
    assert len(trans_execs) == 1
    assert trans_execs[0]["id"] == e2.id

    # 3. Filter by mode="passive"
    passive_execs = list_executions(principal, mode="passive")
    assert len(passive_execs) == 2

    # 4. Filter by status="queued"
    queued_execs = list_executions(principal, status="queued")
    assert len(queued_execs) == 2

    # 5. Principal with restricted scope lacks translator
    issued_restricted = issue_access_key(
        1,
        "Doc Only Key",
        ["mcp.doc_processor", "mcp.work"],
        ["/tmp"],
    )
    db.session.commit()
    restricted_principal = resolve_access_key(issued_restricted.raw_token)
    assert restricted_principal is not None

    restricted_execs = list_executions(restricted_principal)
    assert len(restricted_execs) == 1
    assert restricted_execs[0]["addon_id"] == "doc_processor"


def test_translator_project_listing(app: Flask):
    p1 = TranslationProject(
        id=str(uuid.uuid4()),
        user_id=1,
        name="Project English to Polish",
        source_lang="en",
        target_lang="pl",
        status="in_progress",
    )
    p2 = TranslationProject(
        id=str(uuid.uuid4()),
        user_id=1,
        name="Project English to German",
        source_lang="en",
        target_lang="de",
        status="completed",
    )
    other_p = TranslationProject(
        id=str(uuid.uuid4()),
        user_id=2,
        name="User 2 project",
        source_lang="en",
        target_lang="fr",
        status="completed",
    )
    db.session.add_all([p1, p2, other_p])
    db.session.commit()

    from unittest.mock import MagicMock
    from iarf_assistant.addons.translator.api_client import TranslatorClient

    client = TranslatorClient(MagicMock(), {"current_user_id": 1})
    projects = client.list_projects()
    assert len(projects) == 2
    names = {p["name"] for p in projects}
    assert names == {"Project English to Polish", "Project English to German"}

    # Filter status
    completed_projects = [
        p for p in projects if str(p.get("status") or "").lower() == "completed"
    ]
    assert len(completed_projects) == 1
    assert completed_projects[0]["name"] == "Project English to German"
