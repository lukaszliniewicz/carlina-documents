from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import importlib.metadata
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest
from flask import Flask
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from carlina import application_companions, application_source, distribution
from carlina.mcp_profiles import (
    default_mcp_scopes,
    effective_mcp_scopes,
    selected_mcp_products,
)
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import (
    ContentPolicy,
    RootProfileBinding,
    RuntimeProfile,
    RuntimeProfileError,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_gateway import (
    DOCUMENT_SERVER_INSTRUCTIONS,
    MCP_DEFAULT_SCOPES,
    SERVER_INSTRUCTIONS,
    CarlinaTokenVerifier,
    create_mcp_application,
    mcp_default_scopes,
    mcp_effective_scopes,
)
from iarf_assistant.core.mcp_gateway_runtime import start_mcp_gateway
from iarf_assistant.core.mcp_jobs import issue_access_key
from iarf_assistant.extensions import db
from iarf_assistant.models import User

_GRANTED_SCOPES = (
    "mcp.access",
    "mcp.doc_processor",
    "mcp.translator",
    "mcp.news_pipeline",
    "mcp.news_scripts",
    "mcp.news_publish",
    "mcp.work",
)
_DOCUMENT_SCOPES = frozenset(
    {"mcp.access", "mcp.doc_processor", "mcp.translator", "mcp.work"}
)


def _unused_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def mcp_gateway_factory(tmp_path: Path):
    applications: list[Flask] = []
    handles = []

    def create(
        profile: str,
        *,
        scopes: tuple[str, ...] = _GRANTED_SCOPES,
        start_server: bool = True,
    ):
        root = tmp_path / profile
        root.mkdir()
        application = Flask(
            f"mcp-profile-{profile}", instance_path=str(root / "instance")
        )
        application.config.update(
            CARLINA_PROFILE=profile,
            SQLALCHEMY_DATABASE_URI=f"sqlite:///{root / 'mcp-profile.db'}",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="mcp-profile-composition-test",
            TESTING=True,
        )
        db.init_app(application)
        application.extensions["carlina_encryption"] = EncryptionService(
            InstallationKeyStore.ephemeral()
        )
        composition = resolve_runtime_composition(profile, environment={})
        application.extensions["carlina_runtime_composition"] = composition
        application.extensions["carlina_profile_binding"] = RootProfileBinding(
            composition.profile,
            ContentPolicy.PLAINTEXT_V1
            if composition.profile is RuntimeProfile.DOCUMENTS
            else ContentPolicy.INSTALLATION_ENVELOPE_V2,
            legacy=False,
        )
        selected_ids = (
            {"doc_processor", "translator", "news_pipeline"}
            if composition.addon_ids is None
            else composition.addon_ids
        )
        application.__dict__["addon_manager"] = SimpleNamespace(
            get_loaded_addon=lambda addon_id: (
                object() if addon_id in selected_ids else None
            )
        )
        applications.append(application)

        with application.app_context():
            db.create_all()
            user = User()
            user.id = 1
            user.email = f"{profile}@example.test"
            user.password_hash = "x"
            db.session.add(user)
            db.session.commit()
            issued = issue_access_key(
                1,
                f"MCP {profile} profile test",
                list(scopes),
                [str(root)],
                response_default_bytes=131_072,
                response_max_bytes=262_144,
            )

        handle = None
        endpoint = None
        if start_server:
            port = _unused_port()
            handle = start_mcp_gateway(application, host="127.0.0.1", port=port)
            handles.append(handle)
            endpoint = f"http://127.0.0.1:{port}/mcp"
        return SimpleNamespace(
            app=application,
            composition=composition,
            endpoint=endpoint,
            key_id=issued.key_id,
            token=issued.raw_token,
            verifier=CarlinaTokenVerifier(application),
            allowed_root=str(root),
        )

    yield create

    for handle in reversed(handles):
        handle.stop()
    for application in applications:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()


def test_canonical_mcp_products_and_scope_order_follow_composition():
    workbench = resolve_runtime_composition("workbench", environment={})
    documents = resolve_runtime_composition("documents", environment={})

    assert selected_mcp_products(workbench) == (
        ("doc_processor", "mcp.doc_processor"),
        ("translator", "mcp.translator"),
        ("news_pipeline", "mcp.news_pipeline"),
    )
    assert selected_mcp_products(documents) == (
        ("doc_processor", "mcp.doc_processor"),
        ("translator", "mcp.translator"),
    )
    assert (
        default_mcp_scopes(workbench)
        == MCP_DEFAULT_SCOPES
        == (
            "mcp.access",
            "mcp.doc_processor",
            "mcp.translator",
            "mcp.news_pipeline",
            "mcp.work",
        )
    )
    assert default_mcp_scopes(documents) == (
        "mcp.access",
        "mcp.doc_processor",
        "mcp.translator",
        "mcp.work",
    )
    assert effective_mcp_scopes(_GRANTED_SCOPES, workbench) == frozenset(
        _GRANTED_SCOPES
    )
    assert effective_mcp_scopes(_GRANTED_SCOPES, documents) == _DOCUMENT_SCOPES


def test_gateway_resolver_uses_validated_extension_and_ignores_ambient_profile(
    monkeypatch,
):
    application = Flask(__name__)
    monkeypatch.setenv("CARLINA_PROFILE", "documents")

    assert mcp_default_scopes(application) == MCP_DEFAULT_SCOPES
    assert mcp_effective_scopes(application, _GRANTED_SCOPES) == frozenset(
        _GRANTED_SCOPES
    )

    documents = resolve_runtime_composition("documents", environment={})
    application.extensions["carlina_runtime_composition"] = documents
    application.config["CARLINA_PROFILE"] = "workbench"
    assert mcp_default_scopes(application) == (
        "mcp.access",
        "mcp.doc_processor",
        "mcp.translator",
        "mcp.work",
    )


def test_gateway_resolver_rejects_invalid_profile_or_composition():
    application = Flask(__name__)
    application.config["CARLINA_PROFILE"] = "unsupported"
    with pytest.raises(RuntimeProfileError):
        mcp_default_scopes(application)

    application.extensions["carlina_runtime_composition"] = object()
    with pytest.raises(TypeError, match="runtime composition extension is invalid"):
        mcp_default_scopes(application)


@pytest.mark.parametrize(
    ("profile", "expected_scopes"),
    [
        ("workbench", frozenset(_GRANTED_SCOPES)),
        ("documents", _DOCUMENT_SCOPES),
    ],
)
def test_token_verification_projects_scopes_without_changing_key_claims(
    mcp_gateway_factory, profile, expected_scopes
):
    gateway = mcp_gateway_factory(profile, start_server=False)

    access_token = gateway.verifier._verify_sync(gateway.token)

    assert access_token is not None
    assert access_token.token == ""
    assert access_token.client_id == gateway.key_id
    assert access_token.subject == "1"
    assert frozenset(access_token.scopes) == expected_scopes
    assert access_token.claims == {
        "owner_user_id": 1,
        "allowed_roots": [gateway.allowed_root],
        "response_default_bytes": 131_072,
        "response_max_bytes": 262_144,
        "issuer": "carlina-local",
    }


async def _inspect_catalog(endpoint: str, token: str, mode: str):
    async with (
        httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}) as http,
        Client(streamable_http_client(endpoint, http_client=http), mode=mode) as client,
    ):
        tools = (await client.list_tools()).tools
        resources = (await client.list_resources()).resources
        prompts = (await client.list_prompts()).prompts
        status = await client.call_tool("carlina_status", {})
        guide_result = await client.read_resource("carlina://guide/passive-work")
        guide = "\n".join(
            text
            for item in guide_result.contents
            if isinstance((text := getattr(item, "text", None)), str)
        )
        return (
            tools,
            resources,
            prompts,
            status,
            client.instructions,
            guide,
        )


@pytest.mark.parametrize(
    "profile,expected_count", [("workbench", 98), ("documents", 84)]
)
@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
def test_profile_catalogs_and_advertised_instructions(
    profile, expected_count, mode, mcp_gateway_factory
):
    gateway = mcp_gateway_factory(profile)

    tools, resources, prompts, status, instructions, guide = asyncio.run(
        _inspect_catalog(gateway.endpoint, gateway.token, mode)
    )

    names = {tool.name for tool in tools}
    assert len(tools) == len(names) == expected_count
    news_tool_names = {name for name in names if name.startswith("carlina_news_")}
    assert len(news_tool_names) == (14 if profile == "workbench" else 0)
    assert {str(resource.uri) for resource in resources} == {
        "carlina://guide/passive-work"
    }
    assert {prompt.name for prompt in prompts} == {"carlina_passive_worker"}
    assert status.is_error is False
    status_content = status.structured_content
    expected_addons = {"doc_processor", "translator"}
    expected_instructions = DOCUMENT_SERVER_INSTRUCTIONS
    if profile == "workbench":
        expected_addons.add("news_pipeline")
        expected_instructions = SERVER_INSTRUCTIONS
    assert {
        product["addon_id"] for product in status_content["products"]
    } == expected_addons
    assert status_content["authorization"]["missing_product_scopes"] == []
    if mode == "legacy":
        assert instructions == expected_instructions
    else:
        assert instructions is None
    assert guide == expected_instructions
    if profile == "documents":
        assert instructions is None or "news" not in instructions.lower()
        assert "news" not in guide.lower()


def test_document_status_missing_scopes_only_lists_selected_products(
    mcp_gateway_factory,
):
    gateway = mcp_gateway_factory(
        "documents",
        scopes=("mcp.access", "mcp.doc_processor", "mcp.news_pipeline", "mcp.work"),
    )

    async def status_for_limited_key():
        async with (
            httpx2.AsyncClient(
                headers={"authorization": f"Bearer {gateway.token}"}
            ) as http,
            Client(
                streamable_http_client(gateway.endpoint, http_client=http)
            ) as client,
        ):
            return await client.call_tool("carlina_status", {})

    status = asyncio.run(status_for_limited_key())
    assert status.is_error is False
    authorization = status.structured_content["authorization"]
    assert authorization["granted_scopes"] == [
        "mcp.access",
        "mcp.doc_processor",
        "mcp.work",
    ]
    assert authorization["missing_product_scopes"] == ["mcp.translator"]
    assert [
        product["addon_id"] for product in status.structured_content["products"]
    ] == ["doc_processor"]


def test_document_gateway_creation_does_not_import_news(monkeypatch):
    application = Flask(__name__)
    application.extensions["carlina_runtime_composition"] = resolve_runtime_composition(
        "documents", environment={}
    )
    original_import = builtins.__import__
    blocked_imports: list[str] = []

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        package = (globals or {}).get("__package__", "")
        full_name = f"{package}.{name}" if level and package else name
        if "mcp_news" in full_name or "news_pipeline" in full_name:
            blocked_imports.append(full_name)
            raise AssertionError(f"Document gateway attempted News import: {full_name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    asgi_app = create_mcp_application(application, host="127.0.0.1", port=45678)

    assert asgi_app is not None
    assert blocked_imports == []


def test_full_instruction_text_matches_task_authorized_profile_contract():
    assert hashlib.sha256(SERVER_INSTRUCTIONS.encode("utf-8")).hexdigest() == (
        "4aee80c878830d6d05383dd0a1d1874875031f5b78db28183b3ae52a7c81f7e6"
    )
    assert "no separate literal confirmation is required" in SERVER_INSTRUCTIONS
    assert "obtain literal\n  user confirmation" not in SERVER_INSTRUCTIONS
    assert "unless the user explicitly permits that alternative" in SERVER_INSTRUCTIONS
    assert (
        "visual review of a helper-generated draft does not satisfy"
        in SERVER_INSTRUCTIONS
    )


def _application_source_package(monkeypatch, tmp_path):
    package = tmp_path / "source-package"
    package.mkdir()
    source = b"synthetic retained application source"
    digest = hashlib.sha256(source).hexdigest()
    version = importlib.metadata.version("carlina")
    revision = "a" * 40
    epoch = 1_791_000_000
    (package / "distribution.json").write_text(
        json.dumps(
            {
                "format": "carlina.application-distribution",
                "schema_version": 1,
                "application_profile": "documents",
                "source_revision": revision,
                "source_date_epoch": epoch,
                "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
            }
        )
    )
    (package / "application-source.json").write_text(
        json.dumps(
            {
                "format": "carlina.application-source",
                "schema_version": 1,
                "application_profile": "documents",
                "application_version": version,
                "source_revision": revision,
                "source_date_epoch": epoch,
                "archive": "application-source.tar.gz",
                "filename": "synthetic-source.tar.gz",
                "byte_length": len(source),
                "sha256": digest,
            }
        )
    )
    source_path = package / "application-source.tar.gz"
    source_path.write_bytes(source)
    monkeypatch.setattr(application_source, "files", lambda _name: package)
    monkeypatch.setattr(distribution, "files", lambda _name: package)
    return SimpleNamespace(
        source=source,
        source_path=source_path,
        digest=digest,
        version=version,
        revision=revision,
    )


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
def test_documents_mcp_serves_exact_version_bound_application_source(
    monkeypatch, tmp_path, mode, mcp_gateway_factory
):
    fixture = _application_source_package(monkeypatch, tmp_path)
    source, digest, version, revision, source_path = (
        fixture.source,
        fixture.digest,
        fixture.version,
        fixture.revision,
        fixture.source_path,
    )
    gateway = mcp_gateway_factory("documents", scopes=("mcp.access",))
    uri = f"carlina://application/source/{digest}"

    async def inspect_source():
        async with (
            httpx2.AsyncClient(
                headers={"authorization": f"Bearer {gateway.token}"}
            ) as http,
            Client(
                streamable_http_client(gateway.endpoint, http_client=http), mode=mode
            ) as client,
        ):
            status = await client.call_tool("carlina_status", {})
            identity = status.structured_content["application"]
            assert identity["application_version"] == version
            assert identity["source_revision"] == revision
            assert identity["source"]["resource_uri"] == uri
            assert identity["source"]["byte_length"] == len(source)
            resources = (await client.list_resources()).resources
            assert uri in {str(resource.uri) for resource in resources}
            result = await client.read_resource(uri)
            assert len(result.contents) == 1
            assert result.contents[0].mime_type == "application/gzip"
            assert base64.b64decode(result.contents[0].blob) == source
            guide = await client.read_resource("carlina://guide/passive-work")
            assert uri in guide.contents[0].text
            with pytest.raises(MCPError, match=r"not found|Not found|Unknown resource"):
                await client.read_resource(f"carlina://application/source/{'b' * 64}")
            source_path.write_bytes(b"tampered retained source")
            with pytest.raises(MCPError, match="Application source is unavailable"):
                await client.read_resource(uri)

    asyncio.run(inspect_source())


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
def test_documents_mcp_exposes_small_authenticated_companion_metadata(
    monkeypatch, tmp_path, mode, mcp_gateway_factory
):
    fixture = _application_source_package(monkeypatch, tmp_path)
    prefix = tmp_path / "installed-prefix"
    companion_path = prefix / "share" / "carlina" / "application-companions.json"
    companion_path.parent.mkdir(parents=True)
    payload = {
        "format": "carlina.application-companions",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": fixture.version,
        "source_revision": fixture.revision,
        "application_source_sha256": fixture.digest,
        "wheel_sha256": "b" * 64,
        "inventory_inputs": [{"platform": "linux-64", "sha256": "c" * 64}],
        "companions": [
            {
                "kind": "dependency_sources",
                "filename": "sources.zip",
                "byte_length": 1234,
                "sha256": "d" * 64,
                "url": "https://downloads.example.test/sources.zip",
                "coverage": "selected",
            }
        ],
    }
    companion_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        application_companions, "sys", SimpleNamespace(prefix=str(prefix))
    )
    gateway = mcp_gateway_factory("documents", scopes=("mcp.access",))
    uri = "carlina://application/companions"

    async def inspect_companions():
        async with httpx2.AsyncClient() as unauthenticated:
            response = await unauthenticated.post(
                gateway.endpoint,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "resources/read",
                    "params": {"uri": uri},
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            assert response.status_code == 401
        async with (
            httpx2.AsyncClient(
                headers={"authorization": f"Bearer {gateway.token}"}
            ) as http,
            Client(
                streamable_http_client(gateway.endpoint, http_client=http), mode=mode
            ) as client,
        ):
            status = await client.call_tool("carlina_status", {})
            metadata = status.structured_content["application"]["companions"]
            assert metadata["resource_uri"] == uri
            assert metadata["coverage"] == "selected"
            assert (
                metadata["downloads"][0]["download_url"]
                == payload["companions"][0]["url"]
            )
            resources = (await client.list_resources()).resources
            assert uri in {str(resource.uri) for resource in resources}
            result = await client.read_resource(uri)
            assert result.contents[0].mime_type == "application/json"
            assert json.loads(result.contents[0].text) == metadata
            assert len(result.contents[0].text) < 4096
            guide = await client.read_resource("carlina://guide/passive-work")
            assert uri in guide.contents[0].text
            companion_path.write_bytes(b'{"private-path": "/private/invalid"}')
            with pytest.raises(
                MCPError, match="Application companions are unavailable"
            ):
                await client.read_resource(uri)
            refreshed = await client.call_tool("carlina_status", {})
            application = refreshed.structured_content["application"]
            assert application["companions"] == {
                "available": False,
                "reason": "invalid_manifest",
            }
            assert application["source"]["available"] is True

    asyncio.run(inspect_companions())
