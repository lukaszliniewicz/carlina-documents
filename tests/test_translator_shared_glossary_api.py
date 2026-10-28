from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from flask import g

from iarf_assistant.addons.translator import routes
from iarf_assistant.addons.translator.api_client import TranslatorClient
from iarf_assistant.addons.translator.shared_glossary import SharedGlossaryError

pytest_plugins = ("test_translator_block_translation",)


def _local_term(client: TranslatorClient, project, source_plan_id: str) -> dict:
    return client.glossary_propose(
        project.id,
        source_plan_id=source_plan_id,
        entries=[
            {
                "source_term": "Gemeinde",
                "target_term": "congregation",
                "sense": "religious",
            }
        ],
        idempotency_key="shared-glossary-local-term",
    )["items"][0]


def _status_payload(result):
    if isinstance(result, tuple):
        response, status = result
        return status, response.get_json()
    return 200, result.get_json()


def test_shared_glossary_client_round_trip_uses_owner_scoped_service(app):
    from test_translator_block_translation import _state

    project, _plan, source_plan, operation = _state()
    client = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    term = _local_term(client, project, source_plan.id)

    created = client.shared_glossary_create(
        project.id,
        name="Religious terms",
        concept_ids=[term["concept_id"]],
        idempotency_key="shared-glossary-create",
    )
    assert created["contract"] == "translator-shared-glossary-v1"
    assert created["authority"] == "advisory"
    asset_id = created["id"]
    asset = client.shared_glossary_get(asset_id, proposal_limit=10)
    shared_term = asset["items"][0]
    assert shared_term["source_term"] == "Gemeinde"
    assert asset["proposals"] == []

    client.glossary_propose(
        project.id,
        source_plan_id=source_plan.id,
        entries=[
            {
                "concept_id": term["concept_id"],
                "expected_revision": term["revision"],
                "source_term": "Gemeinde",
                "target_term": "assembly",
                "sense": "religious",
            }
        ],
        idempotency_key="shared-glossary-local-edit",
    )
    operation.state = "completed"
    operation.lease_expires_at = None
    app.extensions["sqlalchemy"].session.commit()
    project_state = client.shared_glossary_project(project.id)
    assert project_state["binding_version"] == 1
    preview = client.shared_glossary_preview(
        project.id,
        asset_id=asset_id,
        revision=1,
        mode="link",
    )
    changed_row = next(row for row in preview["rows"] if row["id"] == shared_term["id"])
    assert changed_row["status"] == "update"

    applied = client.shared_glossary_apply(
        project.id,
        asset_id=asset_id,
        revision=1,
        mode="link",
        preview_token=preview["preview_token"],
        decisions=[{"id": shared_term["id"], "choice": "local"}],
        idempotency_key="shared-glossary-apply",
    )
    assert applied["target_text_changed"] is False
    assert applied["binding"]["asset_id"] == asset_id
    assert applied["authority"] == "model_advisory"

    contribution = client.shared_glossary_contribute(
        project.id,
        asset_id=asset_id,
        expected_revision=1,
        concept_ids=[term["concept_id"]],
        reason="Use the project's revised preferred rendering.",
        idempotency_key="shared-glossary-contribution",
    )
    assert contribution["state"] == "pending"
    assert contribution["authority"] == "advisory"
    pending = client.shared_glossary_get(asset_id, proposal_limit=10)
    assert pending["proposals"][0]["id"] == contribution["proposal_id"]

    reviewed = client.shared_glossary_review(
        asset_id,
        proposal_id=contribution["proposal_id"],
        expected_revision=1,
        decision="accept",
        idempotency_key="shared-glossary-review",
    )
    assert reviewed["revision"] == 2
    assert reviewed["state"] == "accepted"
    revision_two = client.shared_glossary_get(asset_id, revision=2)
    assert revision_two["items"][0]["target_term"] == "assembly"

    detached = client.shared_glossary_detach(
        project.id,
        expected_version=applied["binding_version"],
        idempotency_key="shared-glossary-detach",
    )
    assert detached["binding"] is None
    assert client.shared_glossary_project(project.id)["binding"] is None


def test_shared_glossary_client_rejects_foreign_asset_without_disclosing_it(app):
    from test_translator_block_translation import _state

    project, _plan, source_plan, _operation = _state()
    owner = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    term = _local_term(owner, project, source_plan.id)
    asset = owner.shared_glossary_create(
        project.id,
        name="Private terms",
        concept_ids=[term["concept_id"]],
        idempotency_key="shared-glossary-private-create",
    )

    foreign = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id + 100})
    with pytest.raises(SharedGlossaryError) as raised:
        foreign.shared_glossary_get(asset["id"])
    assert raised.value.code == "shared-glossary-asset-not-found"

    with (
        app.test_request_context(f"/addons/translator/api/shared-glossaries/{asset['id']}"),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(routes.blueprint, "addon_context", SimpleNamespace(), raising=False)
        patch.setattr(
            routes,
            "current_user",
            SimpleNamespace(id=project.user_id + 100, is_authenticated=True),
        )
        result = routes.shared_glossary_get_route.__wrapped__(asset["id"])
    status, body = _status_payload(result)
    assert status == 404
    assert body["code"] == "shared-glossary-asset-not-found"
    assert "Gemeinde" not in str(body)


def test_shared_glossary_http_rejects_nonowners_and_unknown_input(app, monkeypatch):
    project = SimpleNamespace(user_id=7)

    with app.test_request_context(
        "/addons/translator/api/projects/project-1/shared-glossary",
        method="GET",
    ):
        monkeypatch.setattr(
            routes,
            "current_user",
            SimpleNamespace(id=7, is_authenticated=True),
        )
        g.project = project
        g.access_role = "reviewer"
        result = routes.shared_glossary_project_route.__wrapped__("project-1")
    status, body = _status_payload(result)
    assert status == 403
    assert body["code"] == "shared-glossary-owner-only"

    client = Mock()
    with app.test_request_context(
        "/addons/translator/api/shared-glossaries?offset=0&unsupported=1",
        method="GET",
    ):
        g.access_role = "owner"
        monkeypatch.setattr(
            routes,
            "current_user",
            SimpleNamespace(id=7, is_authenticated=True),
        )
        monkeypatch.setattr(routes, "_shared_glossary_client", Mock(return_value=client))
        result = routes.shared_glossary_list_route.__wrapped__()
    status, body = _status_payload(result)
    assert status == 400
    assert body["code"] == "shared-glossary-query-invalid"
    client.shared_glossary_list.assert_not_called()

    with app.test_request_context(
        "/addons/translator/api/shared-glossaries",
        method="POST",
        json={
            "project_id": "project-1",
            "name": "Terms",
            "concept_ids": [],
            "idempotency_key": "key-1",
            "unrecognized": True,
        },
    ):
        g.access_role = "owner"
        monkeypatch.setattr(
            routes,
            "current_user",
            SimpleNamespace(id=7, is_authenticated=True),
        )
        result = routes.shared_glossary_create_route.__wrapped__()
    status, body = _status_payload(result)
    assert status == 400
    assert body["code"] == "shared-glossary-request-invalid"
