"""Owner and revision fences for Translator numbering transport."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask, g

from carlina.runtime_composition import resolve_runtime_composition
from iarf_assistant.addons.translator import (
    api_client,
    numbering_sync,
    operations,
    review_coverage,
    routes,
    worker,
)
from iarf_assistant.addons.translator.models import (
    TranslationNumberingOverlay,
    TranslationOperation,
    TranslationProject,
    TranslationSourcePlan,
    TranslationSourcePlanActive,
)
from iarf_assistant.core import mcp_gateway
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import User

PROVIDER_REFERENCE = {
    "version": 1,
    "capability": "document.semantic_export",
    "provider_addon_id": "doc_processor",
    "provider_capability_id": "semantic_export",
}


class _Queue:
    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []

    def enqueue(self, handler, payload):
        job_id = f"numbering-job-{len(self.jobs) + 1}"
        self.jobs.append((handler, payload))
        return job_id

    def get_status(self, _job_id):
        return {"status": "queued"}


@pytest.fixture
def transport_app(tmp_path, monkeypatch):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'numbering-transport.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="numbering-transport-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    db.create_all()

    owner = User(email="numbering-owner@example.test", password_hash="test")
    other = User(email="numbering-other@example.test", password_hash="test")
    db.session.add_all([owner, other])
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name="Synthetic numbering transport",
        source_lang="en",
        target_lang="pl",
        document_id="source-numbering-1",
        engine_contract="document-block-v1",
        status="completed",
        source_content_hash="a" * 64,
        settings={
            "semantic_export_reference": PROVIDER_REFERENCE,
        },
    )
    db.session.add(project)
    db.session.flush()
    source_plan = TranslationSourcePlan(
        project_id=project.id,
        contract_version="document-block-v1",
        source_document_id="source-numbering-1",
        source_language="en",
        language_profile="default",
        namespace="numbering-namespace",
        snapshot_contract_version="snapshot-v1",
        semantic_contract_version="semantic-html-v1",
        semantic_contract_mode="strict",
        semantic_content_hash="b" * 64,
        source_content_hash="a" * 64,
        snapshot_captured_at=utc_now_db(),
        planner_version="planner-v1",
        planner_settings_hash="c" * 64,
        plan_hash="d" * 64,
        source_attestation={},
        planner_settings={},
        coverage_witness={},
        block_count=0,
        anchor_count=0,
    )
    db.session.add(source_plan)
    db.session.flush()
    db.session.add(
        TranslationSourcePlanActive(
            project_id=project.id, source_plan_id=source_plan.id, version=1
        )
    )
    db.session.add(
        TranslationNumberingOverlay(
            project_id=project.id,
            owner_user_id=owner.id,
            source_plan_id=source_plan.id,
            source_plan_hash=source_plan.plan_hash,
            revision=4,
            payload={"entries": []},
        )
    )
    db.session.commit()
    plan = SimpleNamespace(
        project_id=project.id,
        snapshot=SimpleNamespace(content_hash=project.source_content_hash),
    )
    monkeypatch.setattr(operations, "rehydrate_source_plan", lambda _record: plan)
    # This transport fixture deliberately stubs native source planning.
    monkeypatch.setattr(review_coverage, "snapshot_export_review", lambda _project, requirement="none": {"requirement": requirement, "sha256": "e" * 64})
    monkeypatch.setattr(
        api_client,
        "load_block_source_plan",
        lambda _project: SimpleNamespace(record=source_plan, plan=plan),
    )
    queue = _Queue()

    def provider_factory(_reference):
        return SimpleNamespace(provider_reference=lambda: PROVIDER_REFERENCE)

    client = api_client.TranslatorClient(
        SimpleNamespace(get_task_queue=lambda _addon_id: queue),
        {"current_user_id": owner.id},
        semantic_export_provider_factory=provider_factory,
    )
    application.addon_manager = SimpleNamespace(
        get_loaded_addon=lambda _addon_id: SimpleNamespace(context=object())
    )
    try:
        yield SimpleNamespace(
            app=application,
            owner=owner,
            other=other,
            project=project,
            source_plan=source_plan,
            plan=plan,
            queue=queue,
            client=client,
        )
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _request_context(app, method: str, path: str, *, json=None):
    return app.test_request_context(path, method=method, json=json)


@pytest.mark.parametrize(
    ("handler_name", "method", "body", "client_method", "expected_kwargs"),
    [
        (
            "numbering_review",
            "GET",
            None,
            "numbering_preview",
            {},
        ),
        (
            "numbering_review",
            "POST",
            {
                "preview_token": "a" * 64,
                "selected_ids": ["change-1"],
                "idempotency_key": "numbering-apply-1",
            },
            "numbering_apply",
            {
                "preview_token": "a" * 64,
                "selected_ids": ["change-1"],
                "idempotency_key": "numbering-apply-1",
            },
        ),
        (
            "numbering_review_undo",
            "POST",
            {
                "preview_token": "b" * 64,
                "idempotency_key": "numbering-undo-1",
            },
            "numbering_undo",
            {
                "preview_token": "b" * 64,
                "idempotency_key": "numbering-undo-1",
            },
        ),
    ],
)
def test_owner_http_numbering_routes_dispatch_exact_client_bodies(
    transport_app, handler_name, method, body, client_method, expected_kwargs
):
    client = MagicMock()
    getattr(client, client_method).return_value = {"ok": True}
    route_path = "/addons/translator/api/projects/project-1/numbering"
    if handler_name == "numbering_review_undo":
        route_path += "/undo"

    with _request_context(transport_app.app, method, route_path, json=body):
        g.project = SimpleNamespace(id="project-1", user_id=41)
        g.is_guest = False
        g.access_role = "owner"
        with patch.object(routes, "_numbering_client", return_value=client) as factory:
            response = getattr(routes, handler_name).__wrapped__("project-1")

    if isinstance(response, tuple):
        response, status_code = response
    else:
        status_code = response.status_code
    assert status_code == 200
    assert response.get_json() == {"ok": True}
    factory.assert_called_once_with()
    getattr(client, client_method).assert_called_once_with(
        "project-1", **expected_kwargs
    )


@pytest.mark.parametrize(
    ("handler_name", "method", "body"),
    [
        ("numbering_review", "GET", None),
        (
            "numbering_review",
            "POST",
            {
                "preview_token": "a" * 64,
                "selected_ids": ["change-1"],
                "idempotency_key": "denied-apply",
            },
        ),
        (
            "numbering_review_undo",
            "POST",
            {"preview_token": "b" * 64, "idempotency_key": "denied-undo"},
        ),
    ],
)
@pytest.mark.parametrize(("is_guest", "access_role"), [(True, "owner"), (False, "reviewer")])
def test_http_numbering_routes_reject_guests_and_nonowners_before_client_use(
    transport_app, handler_name, method, body, is_guest, access_role
):
    path = "/addons/translator/api/projects/project-1/numbering"
    if handler_name == "numbering_review_undo":
        path += "/undo"
    with _request_context(transport_app.app, method, path, json=body):
        g.project = SimpleNamespace(id="project-1", user_id=41)
        g.is_guest = is_guest
        g.access_role = access_role
        with patch.object(routes, "_numbering_client") as factory:
            response = getattr(routes, handler_name).__wrapped__("project-1")

    assert response[1] == 403
    assert "owner" in response[0].get_json()["error"].lower()
    factory.assert_not_called()


@pytest.mark.parametrize(
    ("handler_name", "path", "body"),
    [
        (
            "numbering_review",
            "/addons/translator/api/projects/project-1/numbering",
            {
                "preview_token": "a" * 64,
                "selected_ids": ["change-1"],
                "idempotency_key": "strict-apply",
                "owner_user_id": 999,
            },
        ),
        (
            "numbering_review_undo",
            "/addons/translator/api/projects/project-1/numbering/undo",
            {
                "preview_token": "b" * 64,
                "idempotency_key": "strict-undo",
                "owner_user_id": 999,
            },
        ),
    ],
)
def test_http_numbering_mutations_reject_extra_body_keys(
    transport_app, handler_name, path, body
):
    with _request_context(transport_app.app, "POST", path, json=body):
        g.project = SimpleNamespace(id="project-1", user_id=41)
        g.is_guest = False
        g.access_role = "owner"
        with patch.object(routes, "_numbering_client") as factory:
            response, status_code = getattr(routes, handler_name).__wrapped__(
                "project-1"
            )

    assert status_code == 400
    assert "Invalid" in response.get_json()["error"]
    if handler_name == "numbering_review_undo":
        factory.assert_not_called()
    else:
        factory.assert_called_once_with()
        factory.return_value.numbering_apply.assert_not_called()


@pytest.mark.parametrize("mismatch", ["byte_length", "sha256"])
def test_source_numbering_preview_rejects_provider_artifact_integrity_mismatch(
    transport_app, mismatch
):
    source_html = "<html><body><ol start='3'><li>One</li></ol></body></html>"
    data = source_html.encode("utf-8")
    artifact = {
        "format": "html",
        "media_type": "text/html; charset=utf-8",
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    artifact[mismatch] = artifact[mismatch] + 1 if mismatch == "byte_length" else "0" * 64
    provider = SimpleNamespace(
        client=SimpleNamespace(
            semantic_export=MagicMock(
                return_value={"data": data, "artifact": artifact}
            )
        )
    )
    workspace_plan = SimpleNamespace(
        record=object(),
        plan=SimpleNamespace(
            snapshot=SimpleNamespace(metadata={"title": "Synthetic"}, resources=())
        ),
    )

    with _request_context(
        transport_app.app,
        "GET",
        "/addons/translator/api/projects/project-1/numbering/source-preview",
    ):
        g.project = SimpleNamespace(id="project-1", user_id=41, settings={})
        g.is_guest = False
        g.access_role = "owner"
        with (
            patch.object(routes, "load_block_source_plan", return_value=workspace_plan),
            patch.object(
                numbering_sync, "project_source_numbering", return_value=source_html
            ),
            patch.object(routes, "_semantic_export_provider", return_value=provider),
        ):
            response, status_code = routes.numbering_source_preview.__wrapped__(
                "project-1"
            )

    assert status_code == 409
    assert response.get_json()["code"] == "semantic-preview-provider-result"
    request_payload = provider.client.semantic_export.call_args.args[0]
    assert request_payload["semantic_html_sha256"] == hashlib.sha256(
        source_html.encode("utf-8")
    ).hexdigest()


class _ToolServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, description):
        def register(function):
            self.tools[name] = function
            return function

        return register


def test_mcp_numbering_tools_require_translator_scope_and_dispatch_exact_args(
    monkeypatch,
):
    app = Flask(__name__)
    bind_content_policy(app)
    client = SimpleNamespace(
        numbering_preview=MagicMock(return_value={"preview": True}),
        numbering_apply=MagicMock(return_value={"applied": True}),
        numbering_undo=MagicMock(return_value={"undone": True}),
    )
    principal = MagicMock(return_value=SimpleNamespace(owner_user_id=41))
    client_factory = MagicMock(return_value=client)

    async def in_app(_app, function):
        return function()

    monkeypatch.setattr(mcp_gateway, "_principal_from_request", principal)
    monkeypatch.setattr(mcp_gateway, "_translator_client", client_factory)
    monkeypatch.setattr(mcp_gateway, "_in_app", in_app)
    server = _ToolServer()
    mcp_gateway._register_product_tools(
        server,
        app,
        composition=resolve_runtime_composition("workbench", environment={}),
    )

    names = {
        "carlina_translator_numbering_preview",
        "carlina_translator_numbering_apply",
        "carlina_translator_numbering_undo",
    }
    assert names <= server.tools.keys()
    assert asyncio.run(
        server.tools["carlina_translator_numbering_preview"]("project-1")
    ) == {"preview": True}
    assert asyncio.run(
        server.tools["carlina_translator_numbering_apply"](
            "project-1", "a" * 64, ["change-1", "change-2"], "apply-key"
        )
    ) == {"applied": True}
    assert asyncio.run(
        server.tools["carlina_translator_numbering_undo"](
            "project-1", "b" * 64, "undo-key"
        )
    ) == {"undone": True}

    assert principal.call_args_list == [
        call(required_scope="mcp.translator"),
        call(required_scope="mcp.translator"),
        call(required_scope="mcp.translator"),
    ]
    assert client_factory.call_args_list == [call(app, 41)] * 3
    client.numbering_preview.assert_called_once_with("project-1")
    client.numbering_apply.assert_called_once_with(
        "project-1",
        preview_token="a" * 64,
        selected_ids=["change-1", "change-2"],
        idempotency_key="apply-key",
    )
    client.numbering_undo.assert_called_once_with(
        "project-1", preview_token="b" * 64, idempotency_key="undo-key"
    )


def test_http_export_pins_numbering_revision_in_operation_and_queue_payload(
    transport_app,
):
    record = SimpleNamespace(id="plan-1", plan_hash="d" * 64)
    provider = SimpleNamespace(provider_reference=lambda: PROVIDER_REFERENCE)
    operation = SimpleNamespace(kind="document_block_export", queue_job_id="job-1")
    with _request_context(
        transport_app.app,
        "POST",
        "/addons/translator/api/projects/project-1/export?format=epub",
    ):
        g.project = SimpleNamespace(
            id="project-1",
            user_id=41,
            name="Synthetic export",
            target_lang="pl",
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings={"semantic_export_reference": PROVIDER_REFERENCE},
        )
        g.is_guest = False
        with (
            patch.object(routes, "current_user", SimpleNamespace(id=41)),
            patch.object(routes, "is_document_block_engine", return_value=True),
            patch.object(
                routes, "load_block_source_plan", return_value=SimpleNamespace(record=record)
            ),
            patch.object(routes, "_semantic_export_provider", return_value=provider),
            patch("iarf_assistant.addons.translator.numbering_sync.numbering_revision", return_value=7),
            patch.object(routes, "_publish_operation", return_value=(operation, None)) as publish,
            patch.object(routes, "_job_download_url", return_value="/download/job-1"),
            patch.object(routes, "_operation_response", return_value=("ok", 202)),
        ):
            response, status_code = routes.export_project.__wrapped__("project-1")

    assert status_code == 202
    assert response == "ok"
    payload = publish.call_args.kwargs
    assert payload["request_payload"]["numbering_revision"] == 7
    assert payload["queue_payload"]["numbering_revision"] == 7


def test_api_client_pins_numbering_revision_in_operation_and_queue(transport_app):
    result = transport_app.client.export_document(
        transport_app.project.id,
        export_format="epub",
        idempotency_key="numbering-export-1",
    )

    operation = db.session.get(TranslationOperation, result["operation_id"])
    assert operation is not None
    assert operation.checkpoint_json["numbering_revision"] == 4
    assert operation.request_fingerprint == worker.canonical_request_fingerprint(
        operation.checkpoint_json
    )
    handler, payload = transport_app.queue.jobs[0]
    assert handler == "export_document_blocks"
    assert payload["numbering_revision"] == 4
    assert payload["operation_id"] == operation.id
    assert payload["project_id"] == transport_app.project.id


@pytest.mark.parametrize("legacy_payload", [False, True])
def test_worker_rejects_stale_numbering_revision_before_semantic_export(
    transport_app, legacy_payload
):
    project = SimpleNamespace(
        id="project-1",
        user_id=41,
        engine_contract="document-block-v1",
        settings={"semantic_export_reference": PROVIDER_REFERENCE},
    )
    source_plan = SimpleNamespace(
        id="plan-1",
        project_id=project.id,
        plan_hash="d" * 64,
        source_content_hash="a" * 64,
    )
    plan = SimpleNamespace(project_id=project.id)
    operation = SimpleNamespace(
        id="operation-1",
        kind="document_block_export",
        source_plan_id=source_plan.id,
        source_plan_hash=source_plan.plan_hash,
        source_content_hash=source_plan.source_content_hash,
    )
    payload = {
        "project_id": project.id,
        "_operation": operation,
        "format": "epub",
        "export_metadata": {},
        "semantic_export_provider": PROVIDER_REFERENCE,
    }
    if not legacy_payload:
        payload["numbering_revision"] = 3
    fingerprint_payload = {
        "format": "epub",
        "source_content_hash": source_plan.source_content_hash,
        "source_plan_id": source_plan.id,
        "source_plan_hash": source_plan.plan_hash,
        "semantic_export_provider": PROVIDER_REFERENCE,
        "export_metadata": {},
    }
    if not legacy_payload:
        fingerprint_payload["numbering_revision"] = 3
    operation.request_fingerprint = worker.canonical_request_fingerprint(
        fingerprint_payload
    )
    provider = SimpleNamespace(provider_reference=lambda: PROVIDER_REFERENCE)

    with (
        patch.object(worker.db.session, "get", side_effect=[project, source_plan]),
        patch.object(worker, "rehydrate_source_plan", return_value=plan),
        patch.object(worker, "resolve_capability_provider", return_value=provider),
        patch(
            "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
            return_value=4,
        ),
        patch.object(worker, "build_semantic_export_request") as build_request,
        pytest.raises(worker.BlockExportError, match="numbering-export-revision-conflict"),
    ):
        worker.export_document_blocks.__wrapped__(payload, MagicMock(), "job-1")

    build_request.assert_not_called()
