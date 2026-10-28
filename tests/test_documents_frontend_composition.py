"""The Documents shell exposes only destinations supplied by its runtime."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin
from jsonschema import Draft202012Validator

from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.blueprints import register_application_blueprints
from iarf_assistant.core.frontend_contracts import (
    build_frontend_bootstrap,
    evaluate_frontend_feature_flags,
)

_ROOT = Path(__file__).resolve().parents[1]
_UNSUPPORTED_FLAGS = (
    "frontend.chat",
    "frontend.context_inspector",
    "frontend.run_studio",
    "frontend.logs",
)


def _core_status():
    return {
        "status": "built",
        "build_id": "synthetic-core",
        "assets": {
            "entry_url": "/static/frontend/assets/synthetic-core.js",
            "css_urls": [],
        },
    }


def _manager(profile: RuntimeProfile):
    app = Flask("frontend-contract")
    app.extensions["carlina_runtime_composition"] = resolve_runtime_composition(
        profile, environment={}
    )
    addon_ids = ("doc_processor", "translator", "sentence_segmenter")
    return SimpleNamespace(
        app=app,
        manifests={
            addon_id: {
                "id": addon_id,
                "name": addon_id,
                "version": "1.0.0",
                "frontend": {
                    "api_version": "^1.0",
                    "module": "static/dist/application.js",
                },
            }
            for addon_id in addon_ids
        },
        loaded_addons={addon_id: object() for addon_id in addon_ids},
        get_frontend_build_status=lambda _addon_id: {
            "status": "built",
            "rebuild_needed": False,
        },
        get_ui_contribution_manifest=lambda: [
            {
                "id": addon_id,
                "addon_id": addon_id,
                "kind": "task_page",
                "title": addon_id,
                "surfaces": [{"kind": "page", "route": f"/addons/{addon_id}/"}],
            }
            for addon_id in ("doc_processor", "translator")
        ],
        get_ui_component_manifest=list,
    )


@pytest.mark.parametrize("is_admin", [False, True])
def test_documents_bootstrap_has_profile_and_only_available_product_destinations(
    monkeypatch, is_admin
):
    monkeypatch.setattr(
        "iarf_assistant.core.frontend_contracts.get_runtime_core_frontend",
        lambda _root: _core_status(),
    )
    manager = _manager(RuntimeProfile.DOCUMENTS)
    payload = build_frontend_bootstrap(
        user=SimpleNamespace(id=1, email="owner@example.test", is_admin=is_admin),
        manager=manager,
        current_path="/",
        project_root=_ROOT,
        global_flags={},
        environment={},
    )

    assert payload["application"] == {
        "profile": "documents",
        "label": "Documents",
        "home_url": "/",
    }
    assert [item["href"] for item in payload["navigation"]] == [
        "/",
        "/addons/doc_processor/",
        "/addons/translator/",
        "/settings",
    ]
    assert payload["capabilities"]["assistant_services"] is False
    assert payload["capabilities"]["runtime_restart"] is True
    assert all(payload["feature_flags"][flag] is False for flag in _UNSUPPORTED_FLAGS)
    schema = json.loads(
        (_ROOT / "frontend/contracts/frontend-bootstrap.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(payload)


def test_inactive_stale_disabled_and_incompatible_products_have_no_shell_link(
    monkeypatch,
):
    monkeypatch.setattr(
        "iarf_assistant.core.frontend_contracts.get_runtime_core_frontend",
        lambda _root: _core_status(),
    )
    for failure in ("inactive", "stale", "disabled", "incompatible"):
        manager = _manager(RuntimeProfile.DOCUMENTS)
        flags = {}
        if failure == "inactive":
            manager.loaded_addons = {}
        elif failure == "stale":
            manager.get_frontend_build_status = lambda _addon_id: {
                "status": "stale",
                "rebuild_needed": True,
            }
        elif failure == "disabled":
            flags = {
                "frontend.addon.doc_processor": False,
                "frontend.addon.translator": False,
            }
        else:
            for manifest in manager.manifests.values():
                manifest["frontend"]["api_version"] = "^2.0"
        payload = build_frontend_bootstrap(
            user=SimpleNamespace(id=1, is_admin=True),
            manager=manager,
            current_path="/",
            project_root=_ROOT,
            global_flags=flags,
            environment={},
        )
        assert [item["href"] for item in payload["navigation"]] == ["/", "/settings"]


def test_documents_feature_flags_cannot_enable_absent_services():
    flags = evaluate_frontend_feature_flags(
        profile=RuntimeProfile.DOCUMENTS,
        addon_ids=["translator"],
        global_flags=dict.fromkeys(_UNSUPPORTED_FLAGS, True),
        user_settings={"core": {"frontend_feature_flags": {"frontend.chat": True}}},
        environment={},
    )
    assert flags["frontend.shell"]
    assert flags["frontend.addon.translator"]
    assert all(flags[flag] is False for flag in _UNSUPPORTED_FLAGS)
    disabled = evaluate_frontend_feature_flags(
        profile=RuntimeProfile.DOCUMENTS,
        environment={"CARLINA_FRONTEND_DISABLE": "true"},
    )
    assert not any(disabled.values())


def test_documents_root_renders_shell_without_active_products_and_requires_login(
    monkeypatch,
):
    app = Flask("documents-shell", root_path=str(_ROOT / "iarf_assistant"))
    app.secret_key = "synthetic-documents-shell"
    app.extensions["carlina_runtime_composition"] = resolve_runtime_composition(
        "documents", environment={}
    )
    login = LoginManager(app)
    login.login_view = "login"

    class Owner(UserMixin):
        id = "1"
        email = "owner@example.test"
        is_admin = False
        addon_settings: ClassVar[dict[str, object]] = {}

    @login.user_loader
    def load_user(user_id):
        return Owner() if user_id == "1" else None

    monkeypatch.setattr(
        "iarf_assistant.pages.inspect_core_frontend", lambda _root: _core_status()
    )
    monkeypatch.setattr(
        "iarf_assistant.core.frontend_contracts.get_runtime_core_frontend",
        lambda _root: _core_status(),
    )
    register_application_blueprints(app, profile=RuntimeProfile.DOCUMENTS)
    client = app.test_client()
    assert client.get("/").status_code == 302
    with client.session_transaction() as session:
        session["_user_id"] = "1"
        session["_fresh"] = True

    response = client.get("/")
    assert response.status_code == 200
    assert b'data-route-id="documents"' in response.data
    assert response.headers["X-Carlina-Frontend-Implementation"] == "svelte"
    bootstrap = client.get("/api/frontend/bootstrap").get_json()
    assert bootstrap["application"]["profile"] == "documents"
    assert [item["href"] for item in bootstrap["navigation"]] == ["/", "/settings"]
    assert client.get("/api/system/restart-status").status_code == 403
    for path in (
        "/runs",
        "/workspaces",
        "/knowledge/",
        "/logs",
        "/api/frontend/chats",
        "/api/system/docker/status",
    ):
        assert client.get(path).status_code == 404

    monkeypatch.setattr(
        "iarf_assistant.pages.inspect_core_frontend",
        lambda _root: {"status": "missing"},
    )
    assert client.get("/").status_code == 503
