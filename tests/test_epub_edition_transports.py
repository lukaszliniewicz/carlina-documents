from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent

from carlina.core.epub_export import normalize_epub_recipe
from iarf_assistant.addons.doc_processor import (
    epub_editions as document_store,
)
from iarf_assistant.addons.doc_processor import (
    mcp_registration as document_registration,
)
from iarf_assistant.addons.doc_processor import (
    routes as document_routes,
)
from iarf_assistant.addons.doc_processor.mcp_service import DocProcessorMcpService
from iarf_assistant.addons.doc_processor.models import (
    ProcessedDocument,
)
from iarf_assistant.addons.translator import (
    epub_editions as translator_store,
)
from iarf_assistant.addons.translator import (
    routes as translator_routes,
)
from iarf_assistant.addons.translator.api_client import TranslatorClient
from iarf_assistant.addons.translator.mcp_registration import (
    register_translator_tools,
)
from iarf_assistant.addons.translator.models import (
    TranslationProject,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import McpPrincipal
from iarf_assistant.core.mcp_tool_runtime import McpToolRuntime
from iarf_assistant.extensions import db
from iarf_assistant.models import User

REVISION = "a" * 64
NEXT_REVISION = "b" * 64
RECIPE = {
    "epub_options": {"direction": "rtl", "chapter_level": 2},
    "epub_cover": {
        "mode": "pdf_page",
        "page": 3,
        "crop": [0.1234567, 0.1, 0.9, 0.8],
    },
    "epub_image_mode": "auto",
}


@pytest.fixture
def transport_app(tmp_path: Path):
    application = Flask(
        "epub-edition-transports",
        instance_path=str(tmp_path / "instance"),
    )
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'edition-transports.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="epub-edition-transport-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    application.register_blueprint(document_routes.blueprint)
    application.register_blueprint(
        translator_routes.blueprint,
        url_prefix="/addons/translator",
    )
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with application.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="epub-owner@example.test", password_hash="test"),
                User(id=2, email="epub-other@example.test", password_hash="test"),
                ProcessedDocument(
                    id="document-owner",
                    user_id=1,
                    filename="synthetic.html",
                    file_path="synthetic-upload-owner",
                    status="completed",
                    output_html="<main>synthetic owner source</main>",
                    output_markdown="# Synthetic owner source",
                    settings={"document_metadata": {"title": "Owner document"}},
                ),
                ProcessedDocument(
                    id="document-other",
                    user_id=2,
                    filename="synthetic-other.html",
                    file_path="synthetic-upload-other",
                    status="completed",
                    output_html="<main>synthetic other source</main>",
                    output_markdown="# Synthetic other source",
                    settings={"document_metadata": {"title": "Other document"}},
                ),
                TranslationProject(
                    id="project-owner",
                    user_id=1,
                    name="Synthetic owner translation",
                    source_lang="en",
                    target_lang="pl",
                    engine_contract="document-block-v1",
                    document_id="document-owner",
                    source_content_hash=REVISION,
                    settings={"export_metadata": {"title": "Owner translation"}},
                ),
                TranslationProject(
                    id="project-other",
                    user_id=2,
                    name="Synthetic other translation",
                    source_lang="en",
                    target_lang="pl",
                    engine_contract="document-block-v1",
                    document_id="document-other",
                    source_content_hash=NEXT_REVISION,
                    settings={"export_metadata": {"title": "Other translation"}},
                ),
            ]
        )
        db.session.commit()
    try:
        yield application
    finally:
        with application.app_context():
            db.session.remove()
            with db.engine.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
                db.metadata.drop_all(connection)
            db.engine.dispose()


def _login(client, owner_id: int = 1) -> None:
    with client.session_transaction() as session:
        session["_user_id"] = str(owner_id)
        session["_fresh"] = True


def _pin_source_revisions(monkeypatch: pytest.MonkeyPatch, current: list[str]) -> None:
    monkeypatch.setattr(
        document_store,
        "current_source_revision",
        lambda _document: current[0],
    )
    monkeypatch.setattr(
        translator_store,
        "current_source_revision",
        lambda _project: current[0],
    )


def _source_and_settings_snapshot(application: Flask) -> dict[str, object]:
    with application.app_context():
        document = db.session.get(ProcessedDocument, "document-owner")
        project = db.session.get(TranslationProject, "project-owner")
        assert document is not None and project is not None
        return {
            "document": {
                "status": document.status,
                "output_html": document.output_html,
                "output_markdown": document.output_markdown,
                "settings": deepcopy(document.settings),
            },
            "project": {
                "document_id": project.document_id,
                "source_snapshot": deepcopy(project.source_snapshot),
                "source_content_hash": project.source_content_hash,
                "source_captured_at": project.source_captured_at,
                "engine_contract": project.engine_contract,
                "settings": deepcopy(project.settings),
            },
        }


def test_http_edition_endpoints_roundtrip_validate_and_tombstone(
    transport_app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [REVISION]
    _pin_source_revisions(monkeypatch, current)
    client = transport_app.test_client()
    _login(client)
    before = _source_and_settings_snapshot(transport_app)
    normalized_recipe = normalize_epub_recipe(RECIPE)

    for path in (
        "/addons/doc_processor/api/documents/document-owner/epub/edition",
        "/addons/translator/api/projects/project-owner/epub/edition",
    ):
        missing = client.get(path)
        assert missing.status_code == 200
        assert missing.headers["Cache-Control"] == "private, no-store"
        assert missing.get_json()["version"] == 0
        assert missing.get_json()["recipe"] is None
        assert missing.get_json()["current_source_revision"] == REVISION

        saved = client.put(
            path,
            json={
                "recipe": RECIPE,
                "expected_version": 0,
                "expected_source_revision": REVISION,
            },
        )
        assert saved.status_code == 200
        assert saved.headers["Cache-Control"] == "private, no-store"
        assert saved.get_json()["version"] == 1
        assert saved.get_json()["recipe"] == normalized_recipe
        assert saved.get_json()["recipe"]["epub_cover"]["crop"] == [
            0.123457,
            0.1,
            0.9,
            0.8,
        ]

        conflict = client.put(
            path,
            json={
                "recipe": RECIPE,
                "expected_version": 0,
                "expected_source_revision": REVISION,
            },
        )
        assert conflict.status_code == 409
        assert conflict.get_json()["code"] == "epub-edition-conflict"

        current[0] = NEXT_REVISION
        stale = client.put(
            path,
            json={
                "recipe": RECIPE,
                "expected_version": 1,
                "expected_source_revision": REVISION,
            },
        )
        assert stale.status_code == 409
        assert stale.get_json()["code"] == "epub-edition-source-stale"

        assert client.get(path + "?unexpected=1").status_code == 400
        assert (
            client.put(
                path,
                json={
                    "recipe": RECIPE,
                    "expected_version": 1,
                    "expected_source_revision": NEXT_REVISION,
                    "unexpected": "field",
                },
            ).status_code
            == 400
        )
        assert (
            client.put(
                path,
                json={
                    "recipe": RECIPE,
                    "expected_version": True,
                    "expected_source_revision": NEXT_REVISION,
                },
            ).status_code
            == 400
        )
        malformed_boolean = {
            **RECIPE,
            "epub_cover": {"mode": "pdf_page", "page": True},
        }
        assert (
            client.put(
                path,
                json={
                    "recipe": malformed_boolean,
                    "expected_version": 1,
                    "expected_source_revision": NEXT_REVISION,
                },
            ).status_code
            == 400
        )
        oversized = client.put(
            path,
            data=b" " + b"x" * 200_001,
            content_type="application/json",
        )
        assert oversized.status_code == 400

        unchanged = client.get(path)
        assert unchanged.status_code == 200
        assert unchanged.get_json()["version"] == 1
        assert unchanged.get_json()["recipe"] == normalized_recipe
        cleared = client.delete(path, json={"expected_version": 1})
        assert cleared.status_code == 200
        assert cleared.get_json()["version"] == 2
        assert cleared.get_json()["recipe"] is None
        tombstone = client.get(path).get_json()
        assert tombstone["version"] == 2
        assert tombstone["source_revision"] is None
        assert tombstone["recipe"] is None
        current[0] = REVISION

    assert _source_and_settings_snapshot(transport_app) == before


def test_http_authentication_owner_scope_and_translator_guest_editor_denial(
    transport_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_source_revisions(monkeypatch, [REVISION])
    anonymous = transport_app.test_client()
    assert (
        anonymous.get(
            "/addons/doc_processor/api/documents/document-owner/epub/edition"
        ).status_code
        == 401
    )
    assert (
        anonymous.get(
            "/addons/translator/api/projects/project-owner/epub/edition"
        ).status_code
        == 404
    )

    owner = transport_app.test_client()
    _login(owner)
    assert (
        owner.get(
            "/addons/doc_processor/api/documents/document-other/epub/edition"
        ).status_code
        == 404
    )
    assert (
        owner.get(
            "/addons/translator/api/projects/project-other/epub/edition"
        ).status_code
        == 404
    )

    from iarf_assistant.addons.translator import scholarly_http
    from iarf_assistant.addons.translator.scholarly_review import (
        ReviewActor,
        issue_share_grant,
    )

    with transport_app.app_context():
        project = db.session.get(TranslationProject, "project-owner")
        assert project is not None
        _grant, token = issue_share_grant(
            project,
            label="Synthetic reviewer",
            role="reviewer",
            actor=ReviewActor("owner", 1),
        )
        db.session.commit()
    guest = transport_app.test_client()
    reviewer_denied = guest.get(
        "/addons/translator/api/projects/project-owner/epub/edition",
        headers={"X-Translator-Share-Token": token},
    )
    assert reviewer_denied.status_code == 403

    monkeypatch.setattr(
        scholarly_http,
        "_resolve_request_actor",
        lambda _project: (
            SimpleNamespace(role="editor"),
            "editor",
            "Synthetic editor",
            False,
            "owner",
        ),
    )
    editor_denied = owner.get(
        "/addons/translator/api/projects/project-owner/epub/edition"
    )
    assert editor_denied.status_code == 403


def _mcp_runtime(application: Flask, current_owner: list[int]) -> McpToolRuntime:
    async def in_app(app: Flask, function):
        with app.app_context():
            return function()

    def authorize(scope: str) -> McpPrincipal:
        assert scope in {"mcp.doc_processor", "mcp.translator"}
        return McpPrincipal(
            key_id="synthetic-edition-key",
            owner_user_id=current_owner[0],
            scopes=frozenset({scope}),
            allowed_roots=(),
        )

    return McpToolRuntime(
        app=application,
        authorize=authorize,
        in_app=in_app,
        failure=lambda exc: ToolError(f"{type(exc).__name__}: {exc}"),
    )


@pytest.mark.parametrize(
    "tool_name", ["carlina_document_export", "carlina_document_epub_preflight"]
)
def test_document_mcp_image_mode_is_strict_and_forwarded(transport_app, tool_name):
    calls = []
    service = SimpleNamespace(
        export_document=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"success": True}
        ),
        epub_preflight=lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"success": True}
        ),
    )
    server = MCPServer(name="document-image-mode-contract")
    document_registration.register_document_tools(
        server,
        _mcp_runtime(transport_app, [1]),
        service=lambda *_args: service,
        start_execution=lambda *_args, **_kwargs: {},
        normalize_executor_profile=lambda *_args, **_kwargs: None,
        compact_start=lambda value: value,
        image_result=lambda payload: CallToolResult(
            content=[], structuredContent=payload
        ),
        review_edit_failure=lambda *_args: None,
    )
    arguments = {"document_id": "synthetic-document"}
    if tool_name.endswith("export"):
        arguments.update(idempotency_key="mode-request", expected_revision=REVISION)
    asyncio.run(server.call_tool(tool_name, arguments))
    if tool_name.endswith("preflight"):
        assert calls[-1][1]["epub_image_mode"] == "jpeg"
    else:
        assert "epub_image_mode" not in calls[-1][1]
    for mode in ("auto", "jpeg", "webp"):
        asyncio.run(server.call_tool(tool_name, {**arguments, "epub_image_mode": mode}))
        assert calls[-1][1]["epub_image_mode"] == mode
    for invalid in (None, True, 1, [], "preserve", "JPEG"):
        before = len(calls)
        with pytest.raises(ToolError, match="validation"):
            asyncio.run(
                server.call_tool(tool_name, {**arguments, "epub_image_mode": invalid})
            )
        assert len(calls) == before


def test_mcp_edition_wrappers_enforce_actor_and_catalog_witness_schema(
    transport_app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_source_revisions(monkeypatch, [REVISION])
    current_owner = [1]
    runtime = _mcp_runtime(transport_app, current_owner)
    server = MCPServer(name="epub-edition-transport-test")

    document_registration.register_document_tools(
        server,
        runtime,
        service=lambda _app, owner_id: DocProcessorMcpService(None, owner_id),
        start_execution=lambda *_args, **_kwargs: {},
        normalize_executor_profile=lambda *_args, **_kwargs: None,
        compact_start=lambda value: value,
        image_result=lambda payload: CallToolResult(
            content=[TextContent(type="text", text="synthetic")],
            structuredContent=payload,
        ),
        review_edit_failure=lambda *_args: None,
    )
    register_translator_tools(
        server,
        runtime,
        client=lambda app, owner_id: TranslatorClient(
            app,
            {"current_user_id": owner_id},
        ),
        start_execution=lambda *_args, **_kwargs: {},
        normalize_executor_profile=lambda *_args, **_kwargs: None,
        execution_payload=lambda value: {"id": getattr(value, "id", "synthetic")},
        source_pages=lambda *_args, **_kwargs: {},
        image_result=lambda payload: CallToolResult(
            content=[TextContent(type="text", text="synthetic")],
            structuredContent=payload,
        ),
    )

    catalog = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    edition_tool_names = {
        "carlina_document_epub_edition_get",
        "carlina_document_epub_edition_save",
        "carlina_document_epub_edition_clear",
        "carlina_translator_epub_edition_get",
        "carlina_translator_epub_edition_save",
        "carlina_translator_epub_edition_clear",
    }
    assert edition_tool_names <= set(catalog)
    for prefix, resource_key in (
        ("carlina_document", "document_id"),
        ("carlina_translator", "project_id"),
    ):
        get_schema = catalog[f"{prefix}_epub_edition_get"].input_schema
        save_schema = catalog[f"{prefix}_epub_edition_save"].input_schema
        clear_schema = catalog[f"{prefix}_epub_edition_clear"].input_schema
        assert set(get_schema["required"]) == {resource_key}
        assert set(save_schema["required"]) == {
            resource_key,
            "recipe",
            "expected_version",
            "expected_source_revision",
        }
        assert set(clear_schema["required"]) == {resource_key, "expected_version"}
        assert save_schema["properties"]["expected_source_revision"]["pattern"] == (
            "^[0-9a-f]{64}$"
        )
        assert save_schema["properties"]["expected_version"]["minimum"] == 0

    document_get = asyncio.run(
        server.call_tool(
            "carlina_document_epub_edition_get",
            {"document_id": "document-owner"},
        )
    )
    translator_get = asyncio.run(
        server.call_tool(
            "carlina_translator_epub_edition_get",
            {"project_id": "project-owner"},
        )
    )
    assert document_get.structured_content["current_source_revision"] == REVISION
    assert translator_get.structured_content["current_source_revision"] == REVISION

    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "carlina_document_epub_edition_get",
                {"document_id": "document-other"},
            )
        )
    with pytest.raises(ToolError):
        asyncio.run(
            server.call_tool(
                "carlina_translator_epub_edition_get",
                {"project_id": "project-other"},
            )
        )

    for tool_name, key, resource_id in (
        (
            "carlina_document_epub_edition_save",
            "document_id",
            "document-owner",
        ),
        (
            "carlina_translator_epub_edition_save",
            "project_id",
            "project-owner",
        ),
    ):
        saved = asyncio.run(
            server.call_tool(
                tool_name,
                {
                    key: resource_id,
                    "recipe": RECIPE,
                    "expected_version": 0,
                    "expected_source_revision": REVISION,
                },
            )
        )
        assert saved.structured_content["version"] == 1
        assert saved.structured_content["source_revision"] == REVISION
        assert saved.structured_content["recipe"] == normalize_epub_recipe(RECIPE)
