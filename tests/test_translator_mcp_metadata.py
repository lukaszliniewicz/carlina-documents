from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager
from pydantic import ValidationError

from carlina.runtime_composition import resolve_runtime_composition
from iarf_assistant import models
from iarf_assistant.addons.translator import api_client, export_cover
from iarf_assistant.addons.translator.block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
)
from iarf_assistant.addons.translator.block_export import BlockExportError
from iarf_assistant.addons.translator.mcp_settings import (
    ExportMetadataSettings,
    TranslatorSettings,
)
from iarf_assistant.addons.translator.models import (
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.core import mcp_gateway, mcp_jobs
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.upload_paths import resolve_upload_reference
from iarf_assistant.extensions import db


class _Context:
    def get_task_queue(self, addon_id):
        raise AssertionError(f"Unexpected queue access for {addon_id}.")


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-metadata.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-metadata-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(models.User, int(user_id))

    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _user(email: str):
    user = models.User(email=email, password_hash="test")
    db.session.add(user)
    db.session.commit()
    return user


def _project(user, *, metadata=None, version=0):
    project = TranslationProject(
        user_id=user.id,
        name="Synthetic metadata project",
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        source_snapshot="<p>Immutable source.</p>",
        skeleton_html="<p>Immutable target skeleton.</p>",
        settings={
            "source_type": "document",
            "export_metadata": {} if metadata is None else metadata,
            "export_metadata_version": version,
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _client(user_id: int) -> api_client.TranslatorClient:
    return api_client.TranslatorClient(_Context(), {"current_user_id": user_id})


def test_creation_settings_are_typed_and_normalized_for_storage(app):
    settings = TranslatorSettings(
        export_metadata=ExportMetadataSettings(
            title="  New edition  ", authors=["  Original author  "]
        )
    )
    assert settings.export_metadata is not None
    assert settings.export_metadata.title == "  New edition  "

    owner = _user("creation-metadata@example.test")
    stored = _client(owner.id)._settings(
        execution_mode="passive",
        export_metadata=settings.export_metadata.model_dump(exclude_none=True),
    )
    assert stored["export_metadata"] == {
        "title": "New edition",
        "authors": ["Original author"],
    }
    assert stored["export_metadata_version"] == 0


def test_owner_projection_update_retry_and_active_fence(app):
    owner = _user("metadata-owner@example.test")
    other = _user("metadata-other@example.test")
    project = _project(owner, metadata={"title": "Old title"}, version=3)
    client = _client(owner.id)

    before = client.get_document_project(project.id, include_export_metadata=True)
    assert before["export_metadata"] == {
        "version": 3,
        "metadata": {"title": "Old title"},
    }
    assert "source_snapshot" not in before

    updated = client.export_metadata_update(
        project.id,
        {"title": "  New title ", "translators": ["  Translator  "]},
        expected_version=3,
    )
    assert updated == {
        "project_id": project.id,
        "version": 4,
        "metadata": {"title": "New title", "translators": ["Translator"]},
    }
    db.session.refresh(project)
    assert project.settings["export_metadata"] == updated["metadata"]
    assert project.settings["export_metadata_version"] == 4
    assert project.source_snapshot == "<p>Immutable source.</p>"
    assert project.skeleton_html == "<p>Immutable target skeleton.</p>"

    replay = client.export_metadata_update(
        project.id,
        updated["metadata"],
        expected_version=3,
    )
    assert replay == updated

    with pytest.raises(BlockExportError, match="export-metadata-version-conflict"):
        client.export_metadata_update(project.id, {}, expected_version=3)

    active = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="translate",
        idempotency_key="active-metadata-operation",
        request_fingerprint="a" * 64,
        state="queued",
    )
    db.session.add(active)
    db.session.commit()
    assert client.export_metadata_update(project.id, updated["metadata"], 3) == updated
    with pytest.raises(BlockExportError, match="export-metadata-active-operation"):
        client.export_metadata_update(project.id, {"title": "Blocked"}, 4)

    with pytest.raises(LookupError, match="not found"):
        _client(other.id).export_metadata_update(project.id, {}, 4)
    with pytest.raises(LookupError, match="not found"):
        _client(other.id).get_document_project(project.id, include_export_metadata=True)


def test_competing_independent_sqlite_contexts_have_one_cas_winner(app):
    owner = _user("metadata-race@example.test")
    project = _project(owner)
    # Pass scalar identities across threads, never a caller-session ORM object.
    owner_id, project_id = owner.id, project.id
    barrier = threading.Barrier(2)

    def attempt(title: str):
        with app.app_context():
            barrier.wait(timeout=5)
            try:
                result = _client(owner_id).export_metadata_update(
                    project_id, {"title": title}, expected_version=0
                )
                return "ok", result
            except BlockExportError as exc:
                return "error", exc.code
            finally:
                db.session.remove()

    outcomes = [None, None]
    threads = [
        threading.Thread(target=lambda: outcomes.__setitem__(0, attempt("One"))),
        threading.Thread(target=lambda: outcomes.__setitem__(1, attempt("Two"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert all(outcome is not None for outcome in outcomes)
    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("error") == 1
    assert [outcome[1] for outcome in outcomes if outcome[0] == "error"] == [
        "export-metadata-version-conflict"
    ]
    with app.app_context():
        saved = db.session.get(TranslationProject, project_id)
        assert saved.settings["export_metadata_version"] == 1
        assert saved.settings["export_metadata"]["title"] in {"One", "Two"}


def test_extended_metadata_settings_are_closed_and_allow_explicit_clears():
    assert set(ExportMetadataSettings.model_fields) == {
        "title",
        "subtitle",
        "description",
        "publisher",
        "publication_date",
        "rights",
        "journal_or_series",
        "volume",
        "authors",
        "editors",
        "translators",
        "other_contributors",
        "subjects",
        "identifiers",
        "cover_upload_path",
    }
    settings = ExportMetadataSettings(
        subtitle="",
        description="",
        publisher="",
        publication_date="",
        rights="",
        journal_or_series="",
        volume="",
        authors=[],
        editors=[],
        translators=[],
        other_contributors=[],
        subjects=[],
        identifiers=[],
        cover_upload_path="",
    )
    assert settings.model_dump(exclude_none=True)["authors"] == []
    assert settings.model_dump(exclude_none=True)["description"] == ""
    with pytest.raises(ValidationError):
        ExportMetadataSettings(title="")
    with pytest.raises(ValidationError):
        ExportMetadataSettings(other_contributors=[{"name": "A", "role": "EDIT"}])
    with pytest.raises(ValidationError):
        ExportMetadataSettings(identifiers=[{"scheme": "LCCN", "value": "x"}])
    with pytest.raises(ValidationError):
        ExportMetadataSettings(language="en")


def _install_export_cover_capture(monkeypatch, uploads_dir, *, failure=None):
    calls = []

    def capture_export_cover(*, user_id, upload_path):
        calls.append((user_id, upload_path))
        if failure is not None:
            raise failure
        captured_path = resolve_upload_reference(
            upload_path,
            uploads_dir=uploads_dir,
            require_within_uploads=True,
        )
        payload = captured_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        canonical = uploads_dir / str(user_id) / "translator-covers" / f"{digest}.jpg"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_bytes(payload)
        return {
            "upload_path": f"/uploads/{user_id}/translator-covers/{digest}.jpg",
            "media_type": "image/jpeg",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "byte_length": len(payload),
        }

    monkeypatch.setattr(export_cover, "capture_export_cover", capture_export_cover)
    return calls


def test_prepare_export_cover_owner_check_precedes_trusted_path_contract(app):
    owner = _user("cover-owner@example.test")
    stranger = _user("cover-stranger@example.test")
    project = _project(owner)

    with pytest.raises(LookupError, match="not found"):
        _client(stranger.id).prepare_export_cover(project.id, "not-a-Path")


def test_prepare_export_cover_bounded_copy_cleanup_and_reusable_witness(app, tmp_path, monkeypatch):
    owner = _user("cover-import@example.test")
    project = _project(owner)
    uploads_dir = tmp_path / "uploads"
    source = tmp_path / "source.jpg"
    payload = b"synthetic jpeg bytes\x00" * 100
    source.write_bytes(payload)
    monkeypatch.setattr(api_client, "uploads_root", lambda: uploads_dir)
    calls = _install_export_cover_capture(monkeypatch, uploads_dir)

    result = _client(owner.id).prepare_export_cover(project.id, source)

    assert result == {
        "project_id": project.id,
        "cover_upload_path": f"/uploads/{owner.id}/translator-covers/{hashlib.sha256(payload).hexdigest()}.jpg",
        "media_type": "image/jpeg",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_length": len(payload),
    }
    assert calls[0][0] == owner.id
    assert calls[0][1].startswith(f"/uploads/{owner.id}/translator-cover-imports/")
    assert resolve_upload_reference(
        result["cover_upload_path"],
        uploads_dir=uploads_dir,
        require_within_uploads=True,
    ).exists() is True
    import_dir = uploads_dir / str(owner.id) / "translator-cover-imports"
    assert import_dir.is_dir()
    assert list(import_dir.iterdir()) == []
    assert source.read_bytes() == payload
    assert "width" not in result


def test_prepare_export_cover_rejects_oversize_before_copy(app, tmp_path, monkeypatch):
    owner = _user("cover-size@example.test")
    project = _project(owner)
    source = tmp_path / "too-large.jpg"
    source.write_bytes(b"x" * (api_client.MAX_EXPORT_COVER_IMPORT_BYTES + 1))
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setattr(api_client, "uploads_root", lambda: uploads_dir)
    capture = MagicMock(side_effect=AssertionError("capture must not run"))
    module = ModuleType("iarf_assistant.addons.translator.export_cover")
    module.capture_export_cover = capture
    monkeypatch.setitem(sys.modules, module.__name__, module)

    with pytest.raises(BlockExportError, match="cover-upload-too-large"):
        _client(owner.id).prepare_export_cover(project.id, source)
    assert not uploads_dir.exists()
    capture.assert_not_called()


def test_prepare_export_cover_cleans_temp_after_capture_failure(app, tmp_path, monkeypatch):
    owner = _user("cover-failure@example.test")
    project = _project(owner)
    source = tmp_path / "failure.jpg"
    source.write_bytes(b"synthetic cover")
    uploads_dir = tmp_path / "uploads"
    monkeypatch.setattr(api_client, "uploads_root", lambda: uploads_dir)
    calls = _install_export_cover_capture(
        monkeypatch,
        uploads_dir,
        failure=RuntimeError("provider failure with a source path"),
    )

    with pytest.raises(BlockExportError, match="cover-capture-failed"):
        _client(owner.id).prepare_export_cover(project.id, source)
    assert len(calls) == 1
    import_dir = uploads_dir / str(owner.id) / "translator-cover-imports"
    assert list(import_dir.iterdir()) == []


class _ToolServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, description):
        def register(function):
            self.tools[name] = (function, description)
            return function

        return register


def test_gateway_cover_import_authorizes_source_before_client_dispatch(monkeypatch):
    server = _ToolServer()
    application = Flask(__name__)
    bind_content_policy(application)
    principal = SimpleNamespace(owner_user_id=41)
    authorized_path = Path("/authorized/source.jpg")
    client = SimpleNamespace(
        prepare_export_cover=MagicMock(
            return_value={"project_id": "project-1", "cover_upload_path": "/uploads/41/cover.jpg"}
        )
    )

    async def invoke_in_app(_application, function):
        return function()

    monkeypatch.setattr(
        mcp_gateway,
        "_principal_from_request",
        MagicMock(return_value=principal),
    )
    monkeypatch.setattr(
        mcp_gateway,
        "_translator_client",
        MagicMock(return_value=client),
    )
    monkeypatch.setattr(mcp_gateway, "_in_app", invoke_in_app)
    authorize = MagicMock(return_value=authorized_path)
    monkeypatch.setattr(mcp_jobs, "authorize_source_path", authorize)
    mcp_gateway._register_product_tools(
        server,
        application,
        composition=resolve_runtime_composition("workbench", environment={}),
    )

    result = asyncio.run(
        server.tools["carlina_translator_cover_import"][0](
            "project-1", "/requested/source.jpg"
        )
    )

    assert result["cover_upload_path"] == "/uploads/41/cover.jpg"
    authorize.assert_called_once_with(principal, "/requested/source.jpg")
    client.prepare_export_cover.assert_called_once_with("project-1", authorized_path)
    assert "cover_upload_path" in server.tools["carlina_translator_cover_import"][1]
