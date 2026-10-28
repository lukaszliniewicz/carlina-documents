import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import routes
from iarf_assistant.addons.doc_processor.intake import (
    PROFILE_BOOK_QUALITY,
    PROFILE_CUSTOM,
    build_document_settings,
    build_effective_settings,
    validate_pdf_model_readiness,
)
from iarf_assistant.addons.doc_processor.models import (
    DocumentProject,
    ProcessedDocument,
)
from iarf_assistant.addons.doc_processor.project_service import (
    build_effective_settings as project_build_effective_settings,
)
from iarf_assistant.core.model_readiness import ModelReadiness
from iarf_assistant.extensions import db
from iarf_assistant.models import User


def _base_data(**overrides):
    data = {
        "engine": "llm_vision",
        "parsing_model": "provider/parser",
        "metadata_model": "provider/metadata",
        "parallel_pages": 9,
        "append_previous": False,
        "metadata_prepass": False,
        "ignore_footnotes": True,
        "simple_footnotes": True,
        "evaluate_pdf": False,
        "llm_boundary_merge": False,
        "extract_images": False,
        "correct_images": False,
        "skip_cropping": True,
        "prompt_cache": False,
        "removehf": False,
        "metadata_pages": 3,
        "metadata_end_pages": 2,
        "toc_handling": "preserve",
        "store_debug_artifacts": True,
    }
    data.update(overrides)
    return data


def test_missing_profile_preserves_legacy_shape_and_drops_dead_append_next():
    settings = build_document_settings(
        {
            "engine": "pymupdf",
            "append_next": True,
            "store_debug_artifacts": True,
        },
        default_model="provider/default",
    )

    assert "processing_profile" not in settings
    assert "append_next" not in settings["pdf_options"]
    assert settings["pdf_options"]["store_debug_artifacts"] is True
    assert settings["pdf_options"]["append_previous"] is True


def test_book_quality_profile_forces_quality_invariants_and_omits_printed_toc():
    settings = build_document_settings(
        _base_data(profile_id=PROFILE_BOOK_QUALITY),
    )

    options = settings["pdf_options"]
    assert settings["processing_profile"] == PROFILE_BOOK_QUALITY
    assert options["parallel_pages"] == 1
    assert options["append_previous"] is True
    assert options["metadata_prepass"] is True
    assert options["ignore_footnotes"] is False
    assert options["simple_footnotes"] is False
    assert options["evaluate"] is False
    assert options["llm_boundary_merge"] is False
    assert options["extract_images"] is True
    assert options["correct_images"] is True
    assert options["skip_cropping"] is False
    assert options["prompt_cache"] is True
    assert options["removehf"] is True
    assert options["metadata_pages"] == 12
    assert options["metadata_end_pages"] == 4
    assert options["toc_handling"] == "omit"
    assert options["store_debug_artifacts"] is True


def test_book_quality_bounded_metadata_windows_and_printed_toc_is_omitted():
    options = build_document_settings(
        {"profile": PROFILE_BOOK_QUALITY},
    )["pdf_options"]

    assert options["metadata_pages"] == 12
    assert options["metadata_end_pages"] == 4
    assert options["toc_handling"] == "omit"


def test_book_quality_profile_defaults_evaluation_off_without_an_explicit_choice():
    options = build_document_settings(
        {"profile": PROFILE_BOOK_QUALITY},
    )["pdf_options"]

    assert options["evaluate"] is False
    assert options["llm_boundary_merge"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_book_quality_preserves_explicit_boundary_correction_on_reentry(enabled):
    settings = build_document_settings(
        {"profile": PROFILE_BOOK_QUALITY, "llm_boundary_merge": enabled},
    )
    assert settings["pdf_options"]["llm_boundary_merge"] is enabled
    assert project_build_effective_settings(settings)["pdf_options"]["llm_boundary_merge"] is enabled


@pytest.mark.parametrize("evaluate_pdf", [True, False])
def test_book_quality_profile_preserves_explicit_evaluation_choice(evaluate_pdf):
    options = build_document_settings(
        {"profile": PROFILE_BOOK_QUALITY, "evaluate_pdf": evaluate_pdf},
    )["pdf_options"]

    assert options["evaluate"] is evaluate_pdf


def test_custom_profile_cannot_restore_printed_toc_to_canonical_content():
    options = build_document_settings(
        {"profile": PROFILE_CUSTOM, "pdf_options": {"toc_handling": "preserve"}},
    )["pdf_options"]

    assert options["toc_handling"] == "omit"


def test_standalone_and_project_effective_settings_share_profile_contract():
    standalone = routes._build_document_settings(
        _base_data(profile_id=PROFILE_BOOK_QUALITY),
    )
    project = project_build_effective_settings(standalone)

    assert project == standalone


def test_custom_profile_preserves_user_options_and_is_persisted():
    settings = build_document_settings(_base_data(profile_id=PROFILE_CUSTOM))

    assert settings["processing_profile"] == PROFILE_CUSTOM
    assert "profile_id" not in settings
    assert "profile" not in settings
    assert settings["pdf_options"]["parallel_pages"] == 9
    assert settings["pdf_options"]["append_previous"] is False
    assert settings["pdf_options"]["metadata_prepass"] is False
    assert settings["pdf_options"]["metadata_pages"] == 3
    assert settings["pdf_options"]["metadata_end_pages"] == 2
    assert settings["pdf_options"]["store_debug_artifacts"] is True


def test_direct_helper_coerces_string_boolean_options_without_truthiness_bugs():
    settings = build_document_settings(
        {
            "engine": "pymupdf",
            "removehf": "false",
            "append_previous": "false",
            "evaluate_pdf": "true",
            "extract_images": "false",
            "metadata_prepass": "false",
            "store_debug_artifacts": "true",
        }
    )

    options = settings["pdf_options"]
    assert options["removehf"] is False
    assert options["append_previous"] is False
    assert options["evaluate"] is True
    assert options["extract_images"] is False
    assert options["metadata_prepass"] is False
    assert options["store_debug_artifacts"] is True


def test_book_quality_pdf_forces_vision_before_engine_resolution_and_records_request():
    effective_engine = routes._resolve_effective_engine(
        "book.pdf",
        "bogus-engine",
        {"processing_profile": PROFILE_BOOK_QUALITY},
    )

    settings = build_document_settings(
        {
            "processing_profile": PROFILE_BOOK_QUALITY,
            "engine": "bogus-engine",
        },
        effective_engine=effective_engine,
        requested_engine="bogus-engine",
    )

    assert effective_engine == "llm_vision"
    assert settings["engine_id"] == "llm_vision"
    assert settings["requested_engine_id"] == "bogus-engine"
    assert settings["processing_profile"] == PROFILE_BOOK_QUALITY


def test_book_quality_non_pdf_does_not_use_vision_default():
    effective_engine = routes._resolve_effective_engine(
        "notes.txt",
        "llm_vision",
        {"processing_profile": PROFILE_BOOK_QUALITY},
    )

    assert effective_engine == "native"


def test_explicit_engine_catalog_validation_is_fail_closed_but_allows_compatible_native():
    assert routes._resolve_effective_engine("notes.txt", "native", {}) == "native"

    with pytest.raises(ValueError, match="not installed or active"):
        routes._resolve_effective_engine("notes.txt", "does-not-exist", {})

    with pytest.raises(ValueError, match="does not support"):
        routes._resolve_effective_engine("notes.txt", "llm_vision", {})


def test_explicit_active_external_engine_uses_catalog_suffix_contract():
    class ExternalCatalogClient:
        def get_engines(self):
            return [
                {
                    "id": "external_pdf",
                    "formats": ["pdf"],
                    "features": [],
                    "status": "active",
                }
            ]

        def select_engine(self, _filename, *, engine, **_options):
            return engine

        @staticmethod
        def _engine_supports_suffix(engine, suffix):
            return suffix in engine["formats"]

    client = ExternalCatalogClient()
    addon = SimpleNamespace(initialize_client=lambda _config: client)
    with (
        patch.object(routes, "_get_addon", return_value=addon),
        patch.object(routes, "current_user", SimpleNamespace(id=1)),
    ):
        assert routes._resolve_effective_engine("book.pdf", "external_pdf", {}) == (
            "external_pdf"
        )


def test_effective_settings_reapply_quality_profile_after_project_override():
    settings = build_effective_settings(
        build_document_settings({"profile": PROFILE_BOOK_QUALITY}),
        {
            "pdf_options": {
                "parallel_pages": 8,
                "append_previous": False,
                "skip_cropping": True,
                "ignore_footnotes": True,
            }
        },
    )

    options = settings["pdf_options"]
    assert options["parallel_pages"] == 1
    assert options["append_previous"] is True
    assert options["skip_cropping"] is False
    assert options["ignore_footnotes"] is False


def test_readiness_requires_configured_vision_capability_for_pdf():
    not_ready = ModelReadiness(
        model_id="provider/parser",
        ready=False,
        reason="model_not_configured",
        message="The selected model is not configured for this user.",
        provider_name="provider",
        model_type="chat",
        credential_source=None,
    )
    settings = build_document_settings(
        {
            "engine": "llm_vision",
            "parsing_model": "provider/parser",
            "metadata_model": "provider/meta",
        },
    )

    with patch(
        "iarf_assistant.addons.doc_processor.intake.resolve_model_readiness",
        return_value=not_ready,
    ):
        error = validate_pdf_model_readiness("book.pdf", settings, user_id=1)

    assert error is not None
    assert "not configured" in error


def test_passive_pdf_readiness_uses_profile_without_claiming_managed_readiness():
    profile_ready = ModelReadiness(
        model_id="provider/parser",
        ready=True,
        reason=None,
        message="Model profile is configured for external execution.",
        provider_name="provider",
        model_type="chat",
        credential_source=None,
    )
    settings = build_document_settings(
        {
            "engine": "llm_vision",
            "parsing_model": "provider/parser",
            "metadata_model": "provider/meta",
            "metadata_prepass": False,
        }
    )
    with (
        patch(
            "iarf_assistant.addons.doc_processor.intake.resolve_model_profile_readiness",
            return_value=profile_ready,
        ) as profile_readiness,
        patch(
            "iarf_assistant.addons.doc_processor.intake.resolve_model_readiness"
        ) as managed_readiness,
        patch(
            "iarf_assistant.addons.doc_processor.intake.resolve_model_capabilities",
            return_value=SimpleNamespace(supports_vision=True),
        ),
    ):
        assert (
            validate_pdf_model_readiness(
                "book.pdf",
                settings,
                user_id=1,
                require_credential=False,
            )
            is None
        )
    profile_readiness.assert_called_once_with("provider/parser", user_id=1)
    managed_readiness.assert_not_called()


def test_non_pdf_or_non_vision_engine_does_not_require_model_readiness():
    settings = build_document_settings({"engine": "pymupdf"})
    with patch(
        "iarf_assistant.addons.doc_processor.intake.resolve_model_readiness"
    ) as readiness:
        assert validate_pdf_model_readiness("book.pdf", settings, user_id=1) is None
        assert (
            validate_pdf_model_readiness(
                "book.txt", {"engine_id": "llm_vision"}, user_id=1
            )
            is None
        )
    readiness.assert_not_called()


def test_metadata_preview_exposes_runtime_fixed_heading_contract_and_sources():
    app = Flask(__name__)
    bind_content_policy(app)
    app.secret_key = "test"
    app.register_blueprint(routes.blueprint)
    with (
        app.test_request_context(
            "/addons/doc_processor/api/prompts/preview",
            method="POST",
            json={
                "prompts": {"prompt_pdf_metadata": "Editable metadata instructions."},
                "processing_settings": {"processing_profile": PROFILE_BOOK_QUALITY},
            },
        ),
        patch.object(routes, "current_user", SimpleNamespace(id=1)),
        patch.object(
            routes,
            "_get_current_user_addon_settings",
            return_value={},
        ),
    ):
        response = routes.preview_prompts.__wrapped__()

    metadata = response.get_json()["metadata"]
    assert metadata["system_prompt"] == (
        metadata["base_system_prompt"]
        + "\n\n"
        + "Document metadata extraction instructions:\n"
        + "Editable metadata instructions.\n\n"
        + metadata["system_prompt_parts"][3]["text"]
        + "\n\n"
        + metadata["system_prompt_parts"][4]["text"]
        + "\n\n"
        + metadata["system_prompt_parts"][5]["text"]
        + "\n\n"
        + metadata["system_prompt_parts"][6]["text"]
    )
    assert metadata["base_system_prompt"] != "Editable metadata instructions."
    assert metadata["editable_prompt_ids"] == ["prompt_pdf_metadata"]
    assert metadata["system_prompt_parts"][3]["editable"] is False
    assert metadata["system_prompt_parts"][4]["id"] == "native_vision_authoring_contract"
    assert metadata["system_prompt_parts"][4]["editable"] is False
    assert metadata["system_prompt_parts"][5]["id"] == "note_arrangement_contract"
    assert metadata["system_prompt_parts"][5]["editable"] is False
    assert metadata["system_prompt_parts"][6]["id"] == "pdf_cover_candidate_contract"
    assert metadata["system_prompt_parts"][6]["editable"] is False


def test_project_pdf_batch_readiness_failure_has_no_storage_or_database_side_effects():
    with TemporaryDirectory() as temp_dir:
        app = Flask(__name__, root_path=temp_dir, instance_path=temp_dir)
        bind_content_policy(app)
        app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test",
            TESTING=True,
        )
        db.init_app(app)
        with app.app_context():
            db.create_all()
            user = User(id=1, email="intake@example.test", password_hash="test")
            project = DocumentProject(user_id=1, name="Atomic", settings={})
            db.session.add_all([user, project])
            db.session.commit()
            upload_root = Path(temp_dir) / "uploads"
            upload_root.mkdir()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/projects/{project.id}/documents/upload",
                    method="POST",
                    data={
                        "files": [(io.BytesIO(b"pdf bytes"), "book.pdf")],
                        "engine": "llm_vision",
                        "parsing_model": "missing/parser",
                        "metadata_model": "missing/metadata",
                    },
                    content_type="multipart/form-data",
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="missing/default",
                ),
                patch.object(routes, "uploads_root", return_value=upload_root),
            ):
                response, status_code = routes.upload_project_documents.__wrapped__(
                    project.id
                )

            assert status_code == 400
            assert "not configured" in response.get_json()["error"]
            assert db.session.query(ProcessedDocument).count() == 0
            assert db.session.query(DocumentProject).one().documents == []
            assert list(upload_root.rglob("*")) == []

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_standalone_invalid_engine_fails_before_storage_or_database_side_effects():
    with TemporaryDirectory() as temp_dir:
        app = Flask(__name__, root_path=temp_dir, instance_path=temp_dir)
        bind_content_policy(app)
        app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test",
            TESTING=True,
        )
        db.init_app(app)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="standalone@example.test", password_hash="test")
            )
            db.session.commit()
            upload_root = Path(temp_dir) / "uploads"
            upload_root.mkdir()

            with (
                app.test_request_context(
                    "/addons/doc_processor/api/documents",
                    method="POST",
                    data={
                        "file": (io.BytesIO(b"plain text"), "notes.txt"),
                        "engine": "does-not-exist",
                    },
                    content_type="multipart/form-data",
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="provider/default",
                ),
                patch.object(routes, "uploads_root", return_value=upload_root),
            ):
                response, status_code = routes.upload_document.__wrapped__()

            assert status_code == 400
            assert "not installed or active" in response.get_json()["error"]
            assert db.session.query(ProcessedDocument).count() == 0
            assert list(upload_root.rglob("*")) == []

            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_project_book_quality_mixed_batch_selects_vision_only_for_pdf():
    with TemporaryDirectory() as temp_dir:
        app = Flask(__name__, root_path=temp_dir, instance_path=temp_dir)
        bind_content_policy(app)
        app.config.update(
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SECRET_KEY="test",
            TESTING=True,
        )
        db.init_app(app)
        with app.app_context():
            db.create_all()
            user = User(id=1, email="mixed@example.test", password_hash="test")
            project = DocumentProject(user_id=1, name="Mixed", settings={})
            db.session.add_all([user, project])
            db.session.commit()
            upload_root = Path(temp_dir) / "uploads"
            upload_root.mkdir()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/projects/{project.id}/documents/upload",
                    method="POST",
                    data={
                        "files": [
                            (io.BytesIO(b"pdf bytes"), "book.pdf"),
                            (io.BytesIO(b"plain text"), "notes.txt"),
                        ],
                        "engine": "auto",
                        "processing_profile": PROFILE_BOOK_QUALITY,
                        "parsing_model": "provider/parser",
                        "metadata_model": "provider/metadata",
                    },
                    content_type="multipart/form-data",
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="provider/default",
                ),
                patch.object(
                    routes,
                    "validate_pdf_model_readiness_batch",
                    return_value=None,
                ),
                patch.object(routes, "uploads_root", return_value=upload_root),
            ):
                response, status_code = routes.upload_project_documents.__wrapped__(
                    project.id
                )

            assert status_code == 201
            documents = (
                ProcessedDocument.query.filter_by(user_id=1)
                .order_by(ProcessedDocument.created_at.asc())
                .all()
            )
            assert [document.filename for document in documents] == [
                "book.pdf",
                "notes.txt",
            ]
            assert [document.settings["engine_id"] for document in documents] == [
                "llm_vision",
                "native",
            ]
            assert all(
                document.settings["processing_profile"] == PROFILE_BOOK_QUALITY
                for document in documents
            )
            assert all(
                "profile_id" not in document.settings
                and "profile" not in document.settings
                for document in documents
            )
            assert all(
                document.settings["pdf_options"]["metadata_pages"] == 12
                and document.settings["pdf_options"]["metadata_end_pages"] == 4
                for document in documents
            )
            assert response.get_json()["project"]["documents"]

            db.session.remove()
            db.drop_all()
            db.engine.dispose()
