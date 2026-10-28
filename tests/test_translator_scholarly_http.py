"""HTTP and wire-contract tests for the bounded scholarly adapter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from flask import Blueprint, Flask, g, jsonify

from iarf_assistant.addons.translator import scholarly_http as http
from iarf_assistant.addons.translator import scholarly_wire as wire
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
)

PROJECT_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_PROJECT_ID = "123e4567-e89b-12d3-a456-426614174001"


@pytest.fixture
def app() -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY="scholarly-http-test", TESTING=True)
    blueprint = Blueprint("translator", __name__)
    http.register_scholarly_routes(blueprint)

    @blueprint.get("/check/<project_id>")
    @cast(Any, http.project_access_required)
    def check_access(project_id: str):
        return jsonify(
            {
                "role": g.access_role,
                "label": g.access_label,
                "guest": g.is_guest,
                "kind": g.share_auth_kind,
                "has_secret": hasattr(g, "share_session_secret"),
            }
        )

    @blueprint.post("/mutate-check/<project_id>")
    @cast(Any, http.project_access_required)
    def mutate_check(project_id: str):
        return jsonify({"ok": True})

    app.register_blueprint(blueprint, url_prefix="/addons/translator")
    return app


@pytest.fixture
def project() -> SimpleNamespace:
    return SimpleNamespace(
        id=PROJECT_ID,
        user_id=7,
        share_token="legacy-token",
    )


def _anon():
    return SimpleNamespace(is_authenticated=False, id=None)


def _origin_headers() -> dict[str, str]:
    return {"Origin": "http://localhost"}


def test_access_order_and_query_secrets_are_rejected(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda value: project if value == PROJECT_ID else None)
    with patch.object(http, "current_user", SimpleNamespace(is_authenticated=True, id=7)):
        response = app.test_client().get(f"/addons/translator/check/{PROJECT_ID}?token=legacy-token")
    assert response.status_code == 200
    assert response.get_json() == {
        "role": "owner",
        "label": "Owner",
        "guest": False,
        "kind": "owner",
        "has_secret": False,
    }

    with patch.object(http, "current_user", _anon()):
        response = app.test_client().get(f"/addons/translator/check/{PROJECT_ID}?token=legacy-token")
    assert response.status_code == 404

    with patch.object(http, "current_user", _anon()):
        response = app.test_client().get(
            f"/addons/translator/check/{PROJECT_ID}",
            headers={http.LEGACY_SHARE_TOKEN_HEADER: "legacy-token"},
        )
    assert response.status_code == 200
    assert response.get_json()["kind"] == "legacy_header"


def test_grant_header_and_session_cookie_access_are_distinct(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    grant_actor = ReviewActor("reviewer", share_grant_id="grant-1")
    with (
        patch.object(http, "current_user", _anon()),
        patch.object(http.scholarly_review, "resolve_share_grant", return_value=grant_actor),
        patch.object(http, "_grant_label", return_value="A reviewer"),
    ):
        response = app.test_client().get(
            f"/addons/translator/check/{PROJECT_ID}",
            headers={http.SHARE_TOKEN_HEADER: "trg_grant.secret"},
        )
    assert response.status_code == 200
    assert response.get_json()["kind"] == "grant_header"

    session_actor = ReviewActor("viewer", share_grant_id="grant-2")
    client = app.test_client()
    client.set_cookie(http.review_session_cookie_name(PROJECT_ID), "trs_session.secret")
    with (
        patch.object(http, "current_user", _anon()),
        patch.object(http.scholarly_review, "resolve_session", return_value=session_actor),
        patch.object(http, "_grant_label", return_value="A viewer"),
    ):
        response = client.get(f"/addons/translator/check/{PROJECT_ID}")
    assert response.status_code == 200
    assert response.get_json() == {
        "role": "viewer",
        "label": "A viewer",
        "guest": True,
        "kind": "session",
        "has_secret": True,
    }


def test_session_mutation_requires_same_origin_and_csrf(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    actor = ReviewActor("reviewer", share_grant_id="grant-1")
    client = app.test_client()
    client.set_cookie(http.review_session_cookie_name(PROJECT_ID), "trs_session.secret")
    with (
        patch.object(http, "current_user", _anon()),
        patch.object(http.scholarly_review, "resolve_session", return_value=actor),
        patch.object(http, "_grant_label", return_value="Reviewer"),
        patch.object(http.scholarly_review, "validate_session_csrf", return_value=True),
    ):
        assert client.post(f"/addons/translator/mutate-check/{PROJECT_ID}").status_code == 403
        assert client.post(
            f"/addons/translator/mutate-check/{PROJECT_ID}",
            headers={**_origin_headers(), http.CSRF_HEADER: "csrf"},
        ).status_code == 200
        assert client.post(
            f"/addons/translator/mutate-check/{PROJECT_ID}",
            headers={"Origin": "https://evil.example", http.CSRF_HEADER: "csrf"},
        ).status_code == 403


def test_session_cookie_names_are_uuid_isolated():
    first = http.review_session_cookie_name(PROJECT_ID)
    second = http.review_session_cookie_name(OTHER_PROJECT_ID)
    assert first != second
    assert first == f"translator_review_session_{PROJECT_ID.replace('-', '')}"
    with pytest.raises(ValueError):
        http.review_session_cookie_name("not-a-uuid")


def test_wire_projections_are_strict_and_safe():
    note = wire.reader_note(
        {
            "id": "note-1",
            "version": 2,
            "state": "active",
            "body": "A note",
            "citations": ["ref-1", "dossier-note-v1:revision:0", "legacy-target-note-v1:x"],
            "anchor_kind": "block",
            "anchor_text": None,
            "occurrence_index": None,
            "author_type": "reviewer",
            "author_user_id": 999,
            "record_hash": "private",
        }
    )
    assert set(note) == {"id", "version", "state", "body", "body_html", "citation_ids", "anchor", "author"}
    assert note["body_html"] == "A note"
    assert note["citation_ids"] == ["ref-1"]
    assert "author_user_id" not in repr(note)
    assert note["anchor"]["kind"] == "whole_block"

    review = wire.block_review(
        {"decision": "approved", "version": 3, "comment": "Looks good", "author_type": "owner"}
    )
    assert set(review) == {"decision", "version", "rationale", "updated_at", "actor", "effective_decision", "stale", "target_revision_id", "current_target_revision_id", "current_target_version"}
    assert review["rationale"] == "Looks good"
    assert wire.block_review(None)["decision"] == "pending"

    suggestion = wire.glossary_suggestion(
        {"id": "s-1", "source_term": "Dao", "target_term": "Dao", "notes": None,
         "state": "proposed", "version": 1, "created_at": None, "proposer_kind": "reviewer",
         "proposer_grant_id": "private"}
    )
    assert set(suggestion) == {
        "id", "source_term", "target_term", "notes", "state", "version", "created_at", "author"
    }
    assert suggestion["state"] == "pending"
    assert "private" not in repr(suggestion)

    dossier = wire.dossier(
        {"id": "d-1", "project_id": "private-project",
         "source_plan_id": "private-plan", "revision_id": "r-1", "version": 1,
         "scope": {"kind": "blocks", "block_ids": ["b-1"], "record_hash": "x"},
         "provenance": {"method": "external", "request_fingerprint": "x"},
         "payload": {"text": "evidence", "owner_user_id": 9,
                     "terminology": [{"source_term": "private proposal"}],
                     "annotation_proposals": [{"body": "private note"}]},
         "conclusion_provenance": "external_unverified",
         "evidence_verification_state": "unverified"},
        include_payload=True,
    )
    assert "payload" in dossier
    assert "record_hash" not in repr(dossier)
    assert "owner_user_id" not in repr(dossier)
    assert "project_id" not in dossier
    assert "source_plan_id" not in dossier
    assert "private proposal" not in repr(dossier)
    assert "private note" not in repr(dossier)

    owner_dossier = wire.dossier(
        {
            "id": "d-1",
            "payload": {
                "terminology": [{"source_term": "reviewable proposal"}],
                "annotation_proposals": [{"body": "reviewable note"}],
            },
        },
        include_payload=True,
        include_proposals=True,
    )
    assert "reviewable proposal" in repr(owner_dossier)
    assert "reviewable note" in repr(owner_dossier)


def test_grant_and_request_wire_statuses():
    assert wire.grant(
        {"id": "g-1", "label": "Reviewer", "role": "reviewer", "status": "active",
         "revoked_at": "2026-09-01T00:00:00+00:00"}
    )["status"] == "revoked"
    request = wire.retranslation_request(
        {"id": "r-1", "scope": {"kind": "blocks", "block_ids": ["b-1"]},
         "resolved_block_ids": ["b-1"], "custom_prompt": "", "annotation_profile": "none",
         "research_choice": "fresh_document", "dossier_revision_id": None,
         "rationale": "why", "state": "requested", "version": 1}
    )
    assert request["id"] == "r-1"
    assert "operation_id" not in request


def test_redeem_and_bootstrap_contract_never_returns_session_secret(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    actor = ReviewActor("reviewer", share_grant_id="grant-1")
    raw_session = "trs_session.secret"
    first_projection = {"expires_at": "2099-01-01T00:00:00"}
    client = app.test_client()
    with (
        patch.object(http, "current_user", _anon()),
        patch.object(
            http.scholarly_review,
            "redeem_share_grant",
            return_value=(first_projection, raw_session, "csrf-1"),
        ),
        patch.object(http, "_resolve_session_actor", return_value=(actor, None)),
        patch.object(http, "_grant_label", return_value="Reviewer"),
        patch.object(http.db.session, "commit"),
    ):
        response = client.post(
            "/addons/translator/api/share/redeem",
            json={"project_id": PROJECT_ID, "secret": "trg-grant.secret"},
            headers=_origin_headers(),
        )
    assert response.status_code == 200
    assert set(response.get_json()) == {"project_id", "role", "label", "csrf", "expires_at"}
    assert raw_session not in response.get_data(as_text=True)
    cookie = response.headers["Set-Cookie"]
    assert http.review_session_cookie_name(PROJECT_ID) in cookie
    assert "HttpOnly" in cookie and "Path=/addons/translator/" in cookie
    assert "SameSite=Lax" in cookie and "Max-Age=" in cookie

    with (
        patch.object(http, "current_user", _anon()),
        patch.object(http, "_resolve_session_actor", return_value=(actor, None)),
        patch.object(
            http.scholarly_review,
            "rotate_session_csrf",
            return_value=({"expires_at": "2099-01-01T00:00:00"}, "csrf-2"),
        ),
        patch.object(http, "_grant_label", return_value="Reviewer"),
        patch.object(http.db.session, "commit"),
    ):
        response = client.post(
            "/addons/translator/api/share/session",
            json={"project_id": PROJECT_ID},
            headers=_origin_headers(),
        )
    assert response.status_code == 200
    assert response.get_json()["csrf"] == "csrf-2"
    assert raw_session not in response.get_data(as_text=True)


def test_role_matrix_and_no_execute_endpoint(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    assert not any("execute" in rule.rule for rule in app.url_map.iter_rules())
    for role in ("viewer", "reviewer"):
        with (
            patch.object(http, "current_user", _anon()),
            patch.object(
                http,
                "_resolve_request_actor",
                return_value=(ReviewActor(role, share_grant_id="grant-1"), role, role.title(), True, "grant_header"),
            ),
        ):
            client = app.test_client()
            grant_response = client.post(
                f"/addons/translator/api/projects/{PROJECT_ID}/share-grants",
                json={"label": "x", "role": "viewer", "expires_at": None},
                headers={http.SHARE_TOKEN_HEADER: "trg-secret"},
            )
            dossier_response = client.post(
                f"/addons/translator/api/projects/{PROJECT_ID}/research-dossiers",
                json={},
                headers={http.SHARE_TOKEN_HEADER: "trg-secret"},
            )
            resolve_response = client.post(
                f"/addons/translator/api/projects/{PROJECT_ID}/glossary-suggestions/s-1/resolve",
                json={"action": "reject", "expected_version": 1},
                headers={http.SHARE_TOKEN_HEADER: "trg-secret"},
            )
        assert grant_response.status_code == 403
        assert dossier_response.status_code == 403
        assert resolve_response.status_code == 403


def test_viewer_can_inspect_redacted_research_conclusions(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    observed: list[bool] = []

    def dossier_detail(_project, _dossier_id, *, revision_id, include_payload):
        observed.append(include_payload)
        assert revision_id == "revision-1"
        return {
            "id": "dossier-1",
            "revision_id": "revision-1",
            "version": 1,
            "scope": {"kind": "document", "block_ids": []},
            "payload": {
                "document_brief": "Reviewable conclusions.",
                "owner_user_id": 7,
                "terminology": [{"source_term": "Unapproved term"}],
                "annotation_proposals": [{"body": "Unapproved note"}],
            },
        }

    with (
        patch.object(http, "current_user", _anon()),
        patch.object(
            http,
            "_resolve_request_actor",
            return_value=(
                ReviewActor("viewer", share_grant_id="grant-1"),
                "viewer",
                "Study reader",
                True,
                "grant_header",
            ),
        ),
        patch.object(
            http.scholarly_review,
            "get_research_dossier",
            side_effect=dossier_detail,
        ),
    ):
        response = app.test_client().get(
            f"/addons/translator/api/projects/{PROJECT_ID}/research-dossiers/dossier-1"
            "?include_payload=1&revision_id=revision-1",
            headers={http.SHARE_TOKEN_HEADER: "trg-secret"},
        )

    assert response.status_code == 200
    assert observed == [True]
    assert response.get_json()["payload"] == {
        "document_brief": "Reviewable conclusions."
    }
    assert "owner_user_id" not in response.get_data(as_text=True)
    assert "Unapproved term" not in response.get_data(as_text=True)
    assert "Unapproved note" not in response.get_data(as_text=True)


def test_conflict_rolls_back_and_projects_latest_safely(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    latest = {
        "decision": "approved",
        "version": 2,
        "comment": "safe",
        "author_user_id": 99,
        "record_hash": "private",
    }
    with (
        patch.object(http, "current_user", SimpleNamespace(is_authenticated=True, id=7)),
        patch.object(
            http.scholarly_review,
            "record_block_review",
            side_effect=ScholarlyReviewError("review-version-conflict", latest=latest),
        ),
        patch.object(http.db.session, "rollback") as rollback,
    ):
        response = app.test_client().put(
            f"/addons/translator/api/projects/{PROJECT_ID}/blocks/b-1/review",
            headers={http.IDEMPOTENCY_HEADER: "review-conflict-1"},
            json={
                "decision": "approved",
                "rationale": "safe",
                "expected_version": 1,
                "expected_target_version": None,
            },
        )
    assert response.status_code == 409
    assert response.get_json()["latest"] == {
        "decision": "approved",
        "version": 2,
        "rationale": "safe",
        "updated_at": None,
        "actor": {"type": "unknown", "label": "Unknown"},
        "effective_decision": "approved",
        "stale": False,
        "target_revision_id": None,
        "current_target_revision_id": None,
        "current_target_version": None,
    }
    rollback.assert_called_once_with()


def test_reader_note_http_requires_and_forwards_idempotency_key(
    app, project, monkeypatch
):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    path = f"/addons/translator/api/projects/{PROJECT_ID}/blocks/b-1/reader-notes"
    payload = {
        "body": "A reader-facing note.",
        "anchor": {
            "kind": "block",
            "anchor_text": None,
            "occurrence_index": None,
            "target_version": None,
        },
        "citation_ids": [],
    }
    with patch.object(
        http, "current_user", SimpleNamespace(is_authenticated=True, id=7)
    ):
        missing = app.test_client().post(path, json=payload)
    assert missing.status_code == 400
    assert missing.get_json()["code"] == "idempotency-key-required"

    created = {
        "id": "note-1",
        "version": 1,
        "state": "active",
        "body": payload["body"],
        "citations": [],
        "anchor_kind": "block",
        "author_type": "owner",
    }
    with (
        patch.object(http, "current_user", SimpleNamespace(is_authenticated=True, id=7)),
        patch.object(
            http.scholarly_review, "create_reader_note", return_value=created
        ) as create,
        patch.object(http.db.session, "commit"),
    ):
        response = app.test_client().post(
            path,
            json=payload,
            headers={http.IDEMPOTENCY_HEADER: "note-create-1"},
        )
    assert response.status_code == 201
    assert create.call_args.kwargs["idempotency_key"] == "note-create-1"


def test_document_review_is_available_for_partial_project(app, project, monkeypatch):
    project.status = "partial"
    monkeypatch.setattr(http, "_project", lambda _value: project)
    with (
        patch.object(http, "current_user", SimpleNamespace(is_authenticated=True, id=7)),
        patch.object(http.document_review, "review_outline", return_value={"items": []}),
    ):
        response = app.test_client().get(
            f"/addons/translator/api/projects/{PROJECT_ID}/document-review/outline"
        )
    assert response.status_code == 200
    assert response.get_json() == {"items": []}


def test_viewer_cannot_list_editorial_retranslation_requests(app, project, monkeypatch):
    monkeypatch.setattr(http, "_project", lambda _value: project)
    actor = ReviewActor("viewer", share_grant_id="grant-viewer")
    with (
        patch.object(http, "current_user", _anon()),
        patch.object(http.scholarly_review, "resolve_share_grant", return_value=actor),
        patch.object(http, "_grant_label", return_value="Viewer"),
        patch.object(http.scholarly_review, "list_retranslation_requests") as listing,
    ):
        response = app.test_client().get(
            f"/addons/translator/api/projects/{PROJECT_ID}/retranslation-requests",
            headers={http.SHARE_TOKEN_HEADER: "trg_viewer.secret"},
        )
    assert response.status_code == 403
    listing.assert_not_called()
