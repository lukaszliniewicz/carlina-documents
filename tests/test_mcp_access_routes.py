from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from flask import Flask, Response, jsonify
from flask.testing import FlaskClient
from flask_login import LoginManager
from flask_wtf.csrf import generate_csrf

from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.blueprints import register_application_blueprints
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_gateway_runtime import McpGatewayHandle
from iarf_assistant.core.mcp_jobs import issue_access_key
from iarf_assistant.core.web_security import configure_web_security
from iarf_assistant.extensions import db
from iarf_assistant.mcp_access_routes import (
    MCP_DOCUMENT_SCOPES,
    register_mcp_access_blueprint,
)
from iarf_assistant.models import McpAccessKey, User


@pytest.fixture
def access_app(tmp_path: Path) -> Iterator[Flask]:
    database_path = tmp_path / "mcp-access.sqlite3"
    application = Flask(
        "mcp-access-routes",
        instance_path=str(tmp_path / "instance"),
    )
    application.config.update(
        SECRET_KEY="synthetic-mcp-access-test",
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
        CARLINA_ENFORCE_CSRF_IN_TESTS=True,
        WTF_CSRF_ENABLED=True,
    )
    db.init_app(application)
    manager = LoginManager()
    manager.init_app(application)
    configure_web_security(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    register_mcp_access_blueprint(application)

    @application.get("/test/csrf-token")
    def csrf_token() -> Any:
        return jsonify({"csrf_token": generate_csrf()})

    @manager.user_loader
    def load_user(user_id: str) -> User | None:
        try:
            return db.session.get(User, int(user_id))
        except TypeError, ValueError:
            return None

    with application.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(
                    id=1,
                    email="documents-admin@example.test",
                    password_hash="synthetic",
                    is_admin=True,
                ),
                User(
                    id=2,
                    email="other-admin@example.test",
                    password_hash="synthetic",
                    is_admin=True,
                ),
                User(
                    id=3,
                    email="documents-user@example.test",
                    password_hash="synthetic",
                    is_admin=False,
                ),
            ]
        )
        db.session.commit()

    try:
        yield application
    finally:
        with application.app_context():
            db.session.remove()
            db.engine.dispose()


@pytest.fixture
def ready_gateway(access_app: Flask) -> Iterator[None]:
    shutdown = threading.Event()
    thread = threading.Thread(target=shutdown.wait, daemon=True)
    thread.start()
    access_app.extensions["carlina_mcp_gateway"] = McpGatewayHandle(
        host="127.0.0.1",
        port=5006,
        server=type("StartedServer", (), {"started": True})(),
        thread=thread,
    )
    try:
        yield
    finally:
        shutdown.set()
        thread.join(timeout=1)


def _login(client: FlaskClient, user_id: int) -> None:
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def _csrf_headers(client: FlaskClient) -> dict[str, str]:
    response = client.get("/test/csrf-token")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json["csrf_token"]}


def _assert_private_api_headers(response: Response) -> None:
    assert response.headers["Cache-Control"] == "no-store"
    assert "cookie" in {
        value.strip().lower() for value in response.headers.get("Vary", "").split(",")
    }
    assert "X-Carlina-Frontend-API-Version" in response.headers


def _issue_key(application: Flask, owner_user_id: int, label: str) -> str:
    with application.app_context():
        issued = issue_access_key(
            owner_user_id,
            label,
            MCP_DOCUMENT_SCOPES,
            [],
        )
        return issued.key_id


def test_connection_routes_are_registered_only_for_documents_profile() -> None:
    documents = Flask("documents-routes")
    workbench = Flask("workbench-routes")

    register_application_blueprints(documents, profile=RuntimeProfile.DOCUMENTS)
    register_application_blueprints(workbench, profile=RuntimeProfile.WORKBENCH)

    access_methods = set().union(
        *(
            rule.methods
            for rule in documents.url_map.iter_rules()
            if rule.rule == "/api/mcp/access"
        )
    )
    assert {"GET", "POST"} <= access_methods
    assert any(
        rule.rule == "/api/mcp/access/<key_id>" and "DELETE" in rule.methods
        for rule in documents.url_map.iter_rules()
    )
    assert not any(
        rule.rule.startswith("/api/mcp/access")
        for rule in workbench.url_map.iter_rules()
    )


def test_authentication_and_admin_boundary_have_private_headers(
    access_app: Flask,
) -> None:
    client = access_app.test_client()

    anonymous = client.get("/api/mcp/access")
    assert anonymous.status_code in {302, 401}
    _assert_private_api_headers(anonymous)

    _login(client, 3)
    non_admin = client.get("/api/mcp/access")
    assert non_admin.status_code == 403
    assert non_admin.json == {"error": "Administrator access required."}
    _assert_private_api_headers(non_admin)


def test_create_and_list_are_owner_scoped_and_never_list_token_or_digest(
    access_app: Flask,
    ready_gateway,
    tmp_path: Path,
) -> None:
    root = tmp_path / "Über Documents" / "with spaces"
    root.mkdir(parents=True)
    client = access_app.test_client()
    _login(client, 1)

    created = client.post(
        "/api/mcp/access",
        json={"label": "Translator Δ client", "allowed_roots": [str(root), str(root)]},
        headers=_csrf_headers(client),
    )

    assert created.status_code == 201
    _assert_private_api_headers(created)
    assert created.json["scopes"] == list(MCP_DOCUMENT_SCOPES)
    assert created.json["allowed_roots"] == [str(root.resolve())]
    assert created.json["url"] == "http://127.0.0.1:5006/mcp"
    raw_token = created.json["raw_token"]

    with access_app.app_context():
        record = db.session.get(McpAccessKey, created.json["key_id"])
        assert record is not None
        assert record.owner_user_id == 1
        assert record.scopes == list(MCP_DOCUMENT_SCOPES)
        assert record.token_digest != raw_token
        assert raw_token not in json.dumps(
            {
                "key_id": record.key_id,
                "owner_user_id": record.owner_user_id,
                "label": record.label,
                "token_digest": record.token_digest,
                "scopes": record.scopes,
                "allowed_roots": record.allowed_roots,
            }
        )

    other_key_id = _issue_key(access_app, 2, "other admin key")
    listed = client.get("/api/mcp/access")
    assert listed.status_code == 200
    _assert_private_api_headers(listed)
    assert listed.json["scopes"] == list(MCP_DOCUMENT_SCOPES)
    assert [item["key_id"] for item in listed.json["keys"]] == [created.json["key_id"]]
    listed_json = json.dumps(listed.json)
    assert raw_token not in listed_json
    assert record.token_digest not in listed_json
    assert other_key_id not in listed_json
    assert listed.json["keys"][0] == {
        "key_id": created.json["key_id"],
        "label": "Translator Δ client",
        "status": "active",
        "scopes": list(MCP_DOCUMENT_SCOPES),
        "allowed_roots": [str(root.resolve())],
        "created_at": listed.json["keys"][0]["created_at"],
        "last_used_at": None,
        "revoked_at": None,
    }


@pytest.mark.parametrize(
    "hostile_fields",
    [
        {"owner_user_id": 2},
        {"scopes": ["mcp.news_publish"]},
        {"response_max_bytes": 999999999},
    ],
)
def test_creation_rejects_hostile_or_unexpected_fields(
    access_app: Flask,
    ready_gateway,
    tmp_path: Path,
    hostile_fields: dict[str, Any],
) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    client = access_app.test_client()
    _login(client, 1)
    payload = {"label": "allowed", "allowed_roots": [str(root)], **hostile_fields}

    response = client.post(
        "/api/mcp/access", json=payload, headers=_csrf_headers(client)
    )

    assert response.status_code == 400
    assert response.json == {"error": "Check the connection label and import folders."}
    _assert_private_api_headers(response)
    with access_app.app_context():
        assert McpAccessKey.query.count() == 0


@pytest.mark.parametrize(
    "root_factory",
    [
        lambda tmp_path: "relative-folder",
        lambda tmp_path: str(tmp_path / "missing-folder"),
        lambda tmp_path: str(tmp_path / "not-a-folder.txt"),
    ],
    ids=["relative", "missing", "file"],
)
def test_invalid_import_roots_are_refused_without_echo_or_key(
    access_app: Flask,
    ready_gateway,
    tmp_path: Path,
    root_factory: Callable[[Path], str],
) -> None:
    file_path = tmp_path / "not-a-folder.txt"
    file_path.write_text("synthetic", encoding="utf-8")
    invalid_root = root_factory(tmp_path)
    client = access_app.test_client()
    _login(client, 1)

    response = client.post(
        "/api/mcp/access",
        json={"label": "invalid root", "allowed_roots": [invalid_root]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 400
    assert response.json == {"error": "Check the connection label and import folders."}
    assert invalid_root not in response.get_data(as_text=True)
    _assert_private_api_headers(response)
    with access_app.app_context():
        assert McpAccessKey.query.count() == 0


def test_gateway_must_be_ready_before_issuing_key(
    access_app: Flask,
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    access_app.extensions["carlina_mcp_gateway"] = None
    client = access_app.test_client()
    _login(client, 1)

    response = client.post(
        "/api/mcp/access",
        json={"label": "not ready", "allowed_roots": [str(root)]},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 503
    assert response.json == {"error": "The local MCP listener is not ready."}
    _assert_private_api_headers(response)
    with access_app.app_context():
        assert McpAccessKey.query.count() == 0


def test_empty_import_roots_are_allowed_for_existing_documents(
    access_app: Flask,
    ready_gateway,
) -> None:
    client = access_app.test_client()
    _login(client, 1)

    response = client.post(
        "/api/mcp/access",
        json={"label": "existing documents", "allowed_roots": []},
        headers=_csrf_headers(client),
    )

    assert response.status_code == 201
    assert response.json["allowed_roots"] == []
    assert response.json["scopes"] == list(MCP_DOCUMENT_SCOPES)


def test_missing_csrf_token_is_rejected_by_existing_security_boundary(
    access_app: Flask,
    ready_gateway,
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    client = access_app.test_client()
    _login(client, 1)

    response = client.post(
        "/api/mcp/access",
        json={"label": "missing csrf", "allowed_roots": [str(root)]},
    )

    assert response.status_code == 400
    assert response.json == {"error": "CSRF validation failed."}
    _assert_private_api_headers(response)
    with access_app.app_context():
        assert McpAccessKey.query.count() == 0


def test_revoke_is_owner_scoped_and_idempotent(
    access_app: Flask,
) -> None:
    own_key_id = _issue_key(access_app, 1, "own key")
    other_key_id = _issue_key(access_app, 2, "other key")
    client = access_app.test_client()
    _login(client, 1)

    cross_owner = client.delete(
        f"/api/mcp/access/{other_key_id}", headers=_csrf_headers(client)
    )
    assert cross_owner.status_code == 404
    _assert_private_api_headers(cross_owner)
    with access_app.app_context():
        other = db.session.get(McpAccessKey, other_key_id)
        assert other is not None
        assert other.status == "active"
        assert other.revoked_at is None

    first = client.delete(
        f"/api/mcp/access/{own_key_id}", headers=_csrf_headers(client)
    )
    assert first.status_code == 200
    assert first.json == {"key_id": own_key_id, "status": "revoked"}
    _assert_private_api_headers(first)

    repeated = client.delete(
        f"/api/mcp/access/{own_key_id}", headers=_csrf_headers(client)
    )
    assert repeated.status_code == 200
    assert repeated.json == {"key_id": own_key_id, "status": "revoked"}
    _assert_private_api_headers(repeated)
    with access_app.app_context():
        own = db.session.get(McpAccessKey, own_key_id)
        assert own is not None
        assert own.status == "revoked"
        assert own.revoked_at is not None


def test_list_order_uses_newest_then_key_id(access_app: Flask) -> None:
    first_key_id = _issue_key(access_app, 1, "first key")
    second_key_id = _issue_key(access_app, 1, "second key")
    timestamp = datetime(2026, 10, 1, tzinfo=UTC)
    with access_app.app_context():
        for key_id in (first_key_id, second_key_id):
            record = db.session.get(McpAccessKey, key_id)
            assert record is not None
            record.created_at = timestamp
        db.session.commit()

    client = access_app.test_client()
    _login(client, 1)
    response = client.get("/api/mcp/access")

    assert response.status_code == 200
    assert [item["key_id"] for item in response.json["keys"]] == sorted(
        (first_key_id, second_key_id), reverse=True
    )
