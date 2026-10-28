from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, g

from carlina.core.epub_export import (
    epub_review_sha256,
    epub_value_sha256,
    normalize_epub_options,
)
from iarf_assistant.addons.translator import (
    api_client,
    epub_editions,
    export_cover,
    numbering_sync,
    routes,
)
from iarf_assistant.addons.translator.block_export import (
    BlockExportError,
    validate_epub_preflight_result,
)

INPUT_SHA256 = "a" * 64
CONTENT_SHA256 = hashlib.sha256(b"preflight content").hexdigest()
REVIEW_SNAPSHOT = {
    "format": "epub",
    "semantic_html_sha256": INPUT_SHA256,
}
REVIEW_HASH = epub_review_sha256(REVIEW_SNAPSHOT)
PROVIDER_REQUEST = {
    **REVIEW_SNAPSHOT,
    "semantic_html": "<main></main>",
}


def _provider_result(
    options: dict[str, object],
    *,
    input_sha256=INPUT_SHA256,
    review_hash=REVIEW_HASH,
):
    return {
        "options": options,
        "suggested_chapter_level": 2,
        "review_hash": review_hash,
        "outline": {
            "input_sha256": input_sha256,
            "headings": [],
            "tables": [],
            "images": [],
            "pages": [],
        },
        "quality": {
            "schema": "epub-quality-v1",
            "profile": options["profile"],
            "input_sha256": input_sha256,
            "options_sha256": epub_value_sha256(options),
            "artifact_sha256": "0" * 64,
            "content_sha256": CONTENT_SHA256,
            "checks": {
                "structure": "passed",
                "epubcheck": "not_run",
                "ace": "not_run",
                "manual": "not_reviewed",
            },
            "counts": {
                "notes": 0,
                "references": 0,
                "backlinks": 0,
                "headings": 0,
                "images": 0,
                "tables": 0,
                "pages": 0,
            },
            "issues": [],
            "issue_count": 0,
            "epubcheck_version": None,
        },
    }


def test_preflight_result_is_validated_against_exact_html_and_options():
    options = normalize_epub_options({})
    result = _provider_result(options)

    validated = validate_epub_preflight_result(
        result,
        semantic_html_sha256=INPUT_SHA256,
        epub_options=options,
        expected_review_hash=REVIEW_HASH,
    )

    assert validated["options"] == options
    assert validated["quality"]["input_sha256"] == INPUT_SHA256
    assert validated["quality"]["options_sha256"] == epub_value_sha256(options)
    assert validated["review_hash"] == REVIEW_HASH

    for altered in (
        _provider_result(options, input_sha256="b" * 64),
        {
            **result,
            "quality": {
                **result["quality"],
                "options_sha256": "b" * 64,
            },
        },
        _provider_result(options, review_hash="b" * 64),
        {**result, "unexpected": "source text"},
    ):
        with pytest.raises(BlockExportError, match="semantic-export-preflight-result"):
            validate_epub_preflight_result(
                altered,
                semantic_html_sha256=INPUT_SHA256,
                epub_options=options,
                expected_review_hash=REVIEW_HASH,
            )


@pytest.mark.parametrize("mode", [None, "preserve", "JPEG", True, 1, []])
def test_preflight_api_and_http_reject_invalid_image_modes_before_provider_lookup(mode):
    project = SimpleNamespace(
        id="project-1", user_id=7, engine_contract="document-block-v1"
    )
    client = api_client.TranslatorClient(None, {"current_user_id": 7})
    client._project = lambda _project_id: project
    with pytest.raises(BlockExportError, match="epub-image-mode-invalid"):
        client.semantic_export_preflight(
            project.id, epub_options={}, epub_image_mode=mode
        )
    app = Flask(__name__)
    with app.test_request_context(
        "/preflight", method="POST", json={"epub_options": {}, "epub_image_mode": mode}
    ):
        g.project = project
        g.is_guest = False
        g.access_role = "owner"
        with patch.object(routes, "current_user", SimpleNamespace(id=7)):
            response, status = routes.epub_export_preflight.__wrapped__(project.id)
        assert status == 400
        assert response.get_json()["code"] == "epub-image-mode-invalid"


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp", None])
def test_api_client_preflight_uses_effective_metadata_cover_and_current_revision(mode):
    options = normalize_epub_options({"direction": "rtl"})
    canonical_cover = "/uploads/7/translator-covers/" + "c" * 64 + ".jpg"
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "semantic_export_reference": {"version": 1},
            "export_metadata": {"title": "Saved", "cover_upload_path": canonical_cover},
        },
    )
    source_plan = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    provider_request = PROVIDER_REQUEST
    request_value = SimpleNamespace(
        semantic_html_sha256=INPUT_SHA256,
        to_dict=lambda: provider_request,
    )
    calls = []
    provider = SimpleNamespace(
        provider_reference=lambda: {"version": 1},
        client=SimpleNamespace(
            semantic_export_preflight=lambda value: (
                calls.append(value) or _provider_result(options)
            )
        ),
    )
    client = api_client.TranslatorClient(None, {"current_user_id": 7})
    client._project = lambda _project_id: project
    client._semantic_export_provider = lambda _reference: provider
    with (
        patch.object(api_client, "load_block_source_plan", return_value=source_plan),
        patch.object(
            api_client, "build_semantic_export_request", return_value=request_value
        ) as build,
        patch.object(api_client, "pin_export_metadata_cover") as capture,
        patch.object(
            export_cover,
            "resolve_export_cover_path",
            return_value="data:image/jpeg;base64,AA==",
        ) as cover,
        patch.object(numbering_sync, "numbering_revision", return_value=4),
        patch.object(epub_editions, "current_source_revision", return_value="d" * 64),
    ):
        result = client.semantic_export_preflight(
            project.id,
            epub_options=options,
            numbering_revision=4,
            **({"epub_image_mode": mode} if mode is not None else {}),
        )

    assert result["quality"]["input_sha256"] == INPUT_SHA256
    assert result["review_hash"] == REVIEW_HASH
    assert result["numbering_revision"] == 4
    assert result["edition_revision"] == "d" * 64
    assert calls == [provider_request]
    assert build.call_args.kwargs["epub_image_mode"] == (mode or "jpeg")
    assert build.call_args.kwargs["export_metadata"] == {
        "title": "Saved",
        "cover_upload_path": canonical_cover,
    }
    assert build.call_args.kwargs["cover_uri"] == "data:image/jpeg;base64,AA=="
    cover.assert_called_once_with(user_id=7, upload_path=canonical_cover)
    capture.assert_not_called()
    assert project.settings["export_metadata"]["title"] == "Saved"


@pytest.mark.parametrize("mode", ["auto", "jpeg", "webp", None])
def test_route_preflight_is_owner_only_and_binds_cover_target_hash(mode):
    app = Flask(__name__)
    options = normalize_epub_options({})
    canonical_cover = "/uploads/7/translator-covers/" + "c" * 64 + ".jpg"
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        engine_contract="document-block-v1",
        settings={
            "export_metadata": {"title": "Saved", "cover_upload_path": canonical_cover}
        },
    )
    source_plan = SimpleNamespace(record=SimpleNamespace(id="plan-1"))
    request_value = SimpleNamespace(
        semantic_html_sha256=INPUT_SHA256,
        to_dict=lambda: PROVIDER_REQUEST,
    )
    provider_calls = []
    provider = SimpleNamespace(
        provider_reference=lambda: {"version": 1},
        client=SimpleNamespace(
            semantic_export_preflight=lambda value: (
                provider_calls.append(value) or _provider_result(options)
            )
        ),
    )
    with app.test_request_context(
        "/addons/translator/api/projects/project-1/epub/preflight",
        method="POST",
        json={
            "epub_options": {},
            "export_metadata": {"title": "Proposed"},
            **({"epub_image_mode": mode} if mode is not None else {}),
        },
    ):
        g.project = project
        g.is_guest = False
        g.access_role = "owner"
        with (
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "is_document_block_engine", return_value=True),
            patch.object(routes, "load_block_source_plan", return_value=source_plan),
            patch.object(routes, "_semantic_export_provider", return_value=provider),
            patch.object(
                routes, "build_semantic_export_request", return_value=request_value
            ) as build,
            patch.object(routes, "pin_export_metadata_cover") as capture,
            patch.object(
                export_cover,
                "resolve_export_cover_path",
                return_value="data:image/jpeg;base64,AA==",
            ) as cover,
            patch.object(numbering_sync, "numbering_revision", return_value=4),
            patch.object(
                epub_editions, "current_source_revision", return_value="d" * 64
            ),
        ):
            response = routes.epub_export_preflight.__wrapped__(project.id)

    assert response.status_code == 200
    assert build.call_args.kwargs["epub_image_mode"] == (mode or "jpeg")
    result = response.get_json()
    assert result["quality"]["input_sha256"] == INPUT_SHA256
    assert result["review_hash"] == REVIEW_HASH
    assert result["numbering_revision"] == 4
    assert result["edition_revision"] == "d" * 64
    assert provider_calls == [PROVIDER_REQUEST]
    assert build.call_args.kwargs["export_metadata"] == {
        "title": "Proposed",
        "cover_upload_path": canonical_cover,
    }
    assert build.call_args.kwargs["cover_uri"] == "data:image/jpeg;base64,AA=="
    cover.assert_called_once_with(user_id=7, upload_path=canonical_cover)
    capture.assert_not_called()
    assert project.settings["export_metadata"]["title"] == "Saved"


def test_route_preflight_rejects_stale_revision_before_provider_call():
    app = Flask(__name__)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        engine_contract="document-block-v1",
        settings={},
    )
    with app.test_request_context(
        "/addons/translator/api/projects/project-1/epub/preflight",
        method="POST",
        json={"epub_options": {}, "numbering_revision": 3},
    ):
        g.project = project
        g.is_guest = False
        g.access_role = "owner"
        with (
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "is_document_block_engine", return_value=True),
            patch.object(numbering_sync, "numbering_revision", return_value=4),
            patch.object(routes, "_semantic_export_provider") as provider,
        ):
            response, status = routes.epub_export_preflight.__wrapped__(project.id)

    assert status == 409
    assert response.get_json()["code"] == "numbering-export-revision-conflict"
    provider.assert_not_called()


def test_route_preflight_rejects_non_owner_before_project_or_provider_work():
    app = Flask(__name__)
    project = SimpleNamespace(id="project-1", user_id=7)
    with app.test_request_context(
        "/addons/translator/api/projects/project-1/epub/preflight",
        method="POST",
        json={"epub_options": {}},
    ):
        g.project = project
        g.is_guest = False
        g.access_role = "owner"
        with (
            patch.object(routes, "current_user", SimpleNamespace(id=8)),
            patch.object(routes, "is_document_block_engine") as block_engine,
            patch.object(routes, "_semantic_export_provider") as provider,
        ):
            _response, status = routes.epub_export_preflight.__wrapped__(project.id)

    assert status == 403
    block_engine.assert_not_called()
    provider.assert_not_called()


def test_route_preflight_rejects_fields_outside_closed_body():
    app = Flask(__name__)
    project = SimpleNamespace(id="project-1", user_id=7)
    with app.test_request_context(
        "/addons/translator/api/projects/project-1/epub/preflight",
        method="POST",
        json={"epub_options": {}, "provider": "untrusted"},
    ):
        g.project = project
        g.is_guest = False
        g.access_role = "owner"
        with (
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "is_document_block_engine", return_value=True),
            patch.object(routes, "_semantic_export_provider") as provider,
        ):
            _response, status = routes.epub_export_preflight.__wrapped__(project.id)

    assert status == 400
    provider.assert_not_called()
