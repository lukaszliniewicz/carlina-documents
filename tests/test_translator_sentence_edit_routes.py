"""HTTP coverage for explicit document-block sentence edits."""

from __future__ import annotations

from importlib import import_module

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager

from iarf_assistant.addons.translator import routes
from iarf_assistant.addons.translator.block_workspace_api import (
    BlockWorkspaceIntegrityError,
    persist_manual_candidate,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User

_workspace_tests = import_module("tests.test_translator_block_workspace_api")


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'sentence-edit-routes.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="sentence-edit-routes-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    application.register_blueprint(routes.blueprint, url_prefix="/addons/translator")
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _sentence_project(name: str = "Sentence route"):
    project = _workspace_tests._project(name)
    project.settings = {
        **project.settings,
        "translation_unit_mode": "aligned_sentences",
    }
    db.session.commit()
    plan, record = _workspace_tests._plan(
        project,
        "<html><body><p>Source one. Source two.</p></body></html>",
    )
    block = plan.blocks[0]
    _workspace_tests._seed_provider_alignment(
        project,
        plan,
        record,
        block,
        "Uno.  Dos.",
        ["Uno.", "Dos."],
        [{"source": [1], "target": [1]}, {"source": [2], "target": [2]}],
    )
    return project, plan, record, block


def _owner_session(client, user_id: int) -> None:
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


def _put_sentence_edit(client, project, block, edit, *, key, version):
    return client.put(
        f"/addons/translator/api/projects/{project.id}/blocks/{block.id}/candidate",
        headers={"Idempotency-Key": key},
        json={"sentence_edit": edit, "expected_active_version": version},
    )


def test_sentence_edit_route_replaces_without_candidate_and_replays(app):
    project, plan, record, block = _sentence_project("Sentence route replace")
    client = app.test_client()
    _owner_session(client, project.user_id)
    edit = {"action": "replace", "sentence_order": 2, "text": "Dwa 😀!"}

    response = _put_sentence_edit(
        client, project, block, edit, key="route-replace", version=1
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["block"]["target"]["active_version"] == 2
    assert payload["block"]["target"]["editor"]["content"] == "Uno.  Dwa 😀!"
    assert (
        payload["block"]["target"]["alignment"]["origin"] == "user_edited"
    )

    replay = _put_sentence_edit(
        client, project, block, edit, key="route-replace", version=1
    )
    assert replay.status_code == 200
    assert replay.get_json() == payload
    assert len(record.target_revisions) == 2
    assert plan.plan_hash == record.plan_hash


def test_sentence_edit_route_split_then_merge_uses_unicode_offset(app):
    project, _plan, _record, block = _sentence_project("Sentence route split")
    client = app.test_client()
    _owner_session(client, project.user_id)

    split = _put_sentence_edit(
        client,
        project,
        block,
        {
            "action": "split",
            "sentence_order": 1,
            "text": "😀 Raz. Dwa.",
            "offset": 7,
        },
        key="route-split",
        version=1,
    )
    assert split.status_code == 200
    assert split.get_json()["block"]["target"]["active_version"] == 2
    assert split.get_json()["block"]["target"]["editor"]["content"] == (
        "😀 Raz. Dwa.  Dos."
    )

    merge = _put_sentence_edit(
        client,
        project,
        block,
        {"action": "merge_next", "sentence_order": 1},
        key="route-merge",
        version=2,
    )
    assert merge.status_code == 200
    merged = merge.get_json()["block"]
    assert merged["target"]["active_version"] == 3
    assert merged["target"]["editor"]["content"] == "😀 Raz. Dwa.  Dos."
    assert merged["target"]["alignment"]["origin"] == "user_edited"


def test_sentence_edit_route_rejects_stale_and_invalid_shapes(app):
    project, _plan, _record, block = _sentence_project("Sentence route errors")
    client = app.test_client()
    _owner_session(client, project.user_id)

    first = _put_sentence_edit(
        client,
        project,
        block,
        {"action": "replace", "sentence_order": 1, "text": "Jeden."},
        key="route-errors-first",
        version=1,
    )
    assert first.status_code == 200

    stale = _put_sentence_edit(
        client,
        project,
        block,
        {"action": "replace", "sentence_order": 1, "text": "Inny."},
        key="route-errors-stale",
        version=1,
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "stale_active_version"

    invalid = client.put(
        f"/addons/translator/api/projects/{project.id}/blocks/{block.id}/candidate",
        headers={"Idempotency-Key": "route-errors-shape"},
        json={
            "candidate": {},
            "sentence_edit": {
                "action": "replace",
                "sentence_order": 1,
                "text": "Conflict.",
            },
            "expected_active_version": 2,
        },
    )
    assert invalid.status_code == 400
    assert "candidate or sentence_edit" in invalid.get_json()["error"]


def test_sentence_edit_route_forbids_viewers_and_foreign_owners(app):
    project, _plan, _record, block = _sentence_project("Sentence route auth")
    project.share_token = "legacy-viewer-token"
    db.session.commit()
    client = app.test_client()
    path = f"/addons/translator/api/projects/{project.id}/blocks/{block.id}/candidate"
    viewer = client.put(
        path,
        headers={
            "Idempotency-Key": "route-viewer",
            "X-Shared-Token": project.share_token,
        },
        json={
            "sentence_edit": {
                "action": "replace",
                "sentence_order": 1,
                "text": "Viewer cannot edit.",
            },
            "expected_active_version": 1,
        },
    )
    assert viewer.status_code == 403

    foreign = _workspace_tests.User(
        email="foreign-sentence-route@example.test", password_hash="test"
    )
    db.session.add(foreign)
    db.session.commit()
    _owner_session(client, foreign.id)
    response = _put_sentence_edit(
        client,
        project,
        block,
        {"action": "replace", "sentence_order": 1, "text": "Foreign."},
        key="route-foreign",
        version=1,
    )
    assert response.status_code == 404


def test_real_manual_api_rejects_candidate_and_restore_sentence_conflicts(app):
    project, plan, record, block = _sentence_project("Sentence route option conflicts")
    candidate = _workspace_tests._candidate(plan, record, block, "Uno.  Dos.")
    edit = {"action": "replace", "sentence_order": 1, "text": "Uno!"}

    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-alignment-option"):
        persist_manual_candidate(
            project,
            block.id,
            candidate,
            1,
            author_user_id=project.user_id,
            sentence_edit=edit,
        )
    with pytest.raises(BlockWorkspaceIntegrityError, match="candidate-alignment-option"):
        persist_manual_candidate(
            project,
            block.id,
            None,
            1,
            author_user_id=project.user_id,
            restore_parent_alignment=True,
            sentence_edit=edit,
        )
