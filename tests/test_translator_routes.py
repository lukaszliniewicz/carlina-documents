import hashlib
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from content_storage_support import bind_content_policy
from carlina.core.epub_export import normalize_epub_options
from flask import Flask, g

from iarf_assistant.addons import translator  # isort: skip

routes = translator.routes
project_deletion = translator.project_deletion
MAX_DOCUMENT_SNAPSHOT_BYTES = translator.source_snapshot.MAX_DOCUMENT_SNAPSHOT_BYTES


class FakeQueue:
    def __init__(self, status=None):
        self.enqueued = []
        self.status = status or {}

    def enqueue(self, task_name, payload):
        self.enqueued.append((task_name, payload))
        return "job-123"

    def get_status(self, job_id):
        return self.status


class TranslatorRouteWorkerBoundaryTests(unittest.TestCase):
    def test_block_research_uses_generous_model_clamped_output_headroom(self):
        self.assertEqual(routes.BLOCK_RESEARCH_BUDGET["max_model_calls"], 128)
        self.assertEqual(routes.BLOCK_RESEARCH_BUDGET["max_tool_calls"], 64)
        self.assertEqual(routes.BLOCK_RESEARCH_BUDGET["output_tokens"], 128_000)

    def test_base_project_settings_normalizes_export_metadata_defaults(self):
        settings = routes._base_project_settings(
            {
                "export_metadata": {
                    "title": "  Saved title ",
                    "authors": [" Author "],
                    "translators": [" Translator "],
                }
            }
        )

        self.assertEqual(
            settings["export_metadata"],
            {
                "title": "Saved title",
                "authors": ["Author"],
                "translators": ["Translator"],
            },
        )

    def test_project_delete_uses_one_sqlite_aggregate_statement(self):
        project = SimpleNamespace(id="project-1")
        dialect = SimpleNamespace(name="sqlite")
        with (
            patch.object(
                routes.db.session,
                "get_bind",
                return_value=SimpleNamespace(dialect=dialect),
            ),
            patch.object(routes.db.session, "execute") as execute,
        ):
            project_deletion.delete_project_aggregate(project)

        self.assertEqual(len(execute.call_args_list), 2)
        self.assertEqual(
            str(execute.call_args_list[0].args[0]), "PRAGMA defer_foreign_keys = ON"
        )
        self.assertIn("DELETE FROM translation_project", str(execute.call_args_list[1].args[0]))
        self.assertEqual(
            execute.call_args_list[1].kwargs["execution_options"],
            {"synchronize_session": False},
        )

    def _app(self):
        app = Flask(__name__)
        bind_content_policy(app)
        app.secret_key = "test"
        app.register_blueprint(routes.blueprint, url_prefix="/addons/translator")
        return app

    def test_block_candidate_requires_and_forwards_idempotency_key(self):
        app = self._app()
        project = SimpleNamespace(id="project-1", user_id=7, engine_contract="document-block-v1")
        saved = {"id": "block-1", "target": {"active_version": 1}}
        with (
            app.test_request_context(
                "/addons/translator/api/projects/project-1/blocks/block-1/candidate",
                method="PUT",
                json={"candidate": {}, "expected_active_version": 0},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "persist_manual_candidate", return_value=saved) as persist,
        ):
            g.project = project
            missing, missing_status = routes.update_block_candidate.__wrapped__(
                "project-1", "block-1"
            )
        self.assertEqual(missing_status, 400)
        self.assertIn("Idempotency-Key", missing.get_json()["error"])

        with (
            app.test_request_context(
                "/addons/translator/api/projects/project-1/blocks/block-1/candidate",
                method="PUT",
                headers={"Idempotency-Key": "target-http-1"},
                json={"candidate": {}, "expected_active_version": 0},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "persist_manual_candidate", return_value=saved) as persist,
        ):
            g.project = project
            response, response_status = routes.update_block_candidate.__wrapped__(
                "project-1", "block-1"
            )
        self.assertEqual(response_status, 200)
        self.assertEqual(response.get_json(), {"success": True, "block": saved})
        self.assertEqual(persist.call_args.kwargs["idempotency_key"], "target-http-1")

    def test_block_candidate_maps_idempotency_conflict_to_http_409(self):
        app = self._app()
        project = SimpleNamespace(id="project-1", user_id=7, engine_contract="document-block-v1")
        with (
            app.test_request_context(
                "/addons/translator/api/projects/project-1/blocks/block-1/candidate",
                method="PUT",
                headers={"Idempotency-Key": "target-http-conflict"},
                json={"candidate": {}, "expected_active_version": 0},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(
                routes,
                "persist_manual_candidate",
                side_effect=routes.BlockWorkspaceIntegrityError(
                    "candidate-idempotency-conflict"
                ),
            ),
        ):
            g.project = project
            response = routes.update_block_candidate.__wrapped__("project-1", "block-1")
        self.assertEqual(response[1], 409)
        self.assertEqual(response[0].get_json()["code"], "candidate-idempotency-conflict")

    def test_block_candidate_maps_source_plan_conflict_to_http_409(self):
        app = self._app()
        project = SimpleNamespace(id="project-1", user_id=7, engine_contract="document-block-v1")
        with (
            app.test_request_context(
                "/addons/translator/api/projects/project-1/blocks/block-1/candidate",
                method="PUT",
                headers={"Idempotency-Key": "target-http-stale-plan"},
                json={"candidate": {}, "expected_active_version": 0},
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(
                routes,
                "persist_manual_candidate",
                side_effect=routes.BlockWorkspaceIntegrityError(
                    "source-plan-active-conflict"
                ),
            ),
        ):
            g.project = project
            response = routes.update_block_candidate.__wrapped__("project-1", "block-1")
        self.assertEqual(response[1], 409)
        self.assertEqual(response[0].get_json()["code"], "source-plan-active-conflict")

    @staticmethod
    def _operation(project_id="project-1", job_id="job-123"):
        now = datetime.now()
        return SimpleNamespace(
            id="operation-1",
            project_id=project_id,
            owner_user_id=7,
            kind="export",
            queue_job_id=job_id,
            state="queued",
            version=1,
            progress=0,
            message="Queued.",
            error_code=None,
            source_content_hash=None,
            cancel_requested_at=None,
            lease_expires_at=None,
            created_at=now,
            started_at=None,
            finished_at=None,
            updated_at=now,
        )

    @staticmethod
    def _operation_query(operation):
        return SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(one_or_none=lambda: operation)
        )

    def test_cancel_route_commits_expired_operation_reconciliation(self):
        app = self._app()
        project = SimpleNamespace(id="project-1", user_id=7)
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(
                first=lambda: project,
            )
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/cancel",
            method="POST",
        ):
            with (
                patch.object(
                    routes,
                    "TranslationProject",
                    SimpleNamespace(query=query),
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(
                    routes,
                    "reconcile_expired_active_operation",
                    return_value=None,
                ),
                patch.object(routes.db.session, "commit") as commit,
            ):
                response = routes.cancel_project_jobs.__wrapped__("project-1")

        self.assertFalse(response.get_json()["success"])
        commit.assert_called_once_with()

    def test_cancel_route_signals_queue_before_durable_write_and_accepts_lock_race(
        self,
    ):
        app = self._app()
        project = SimpleNamespace(id="project-1", user_id=7)
        operation = SimpleNamespace(id="operation-1", queue_job_id="job-1")
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(
                first=lambda: project,
            )
        )
        events = []

        def signal_queue(*_args, **_kwargs):
            events.append("queue")
            return {"status": "cancelling", "cancel_requested": True}

        def fail_durable_write(_operation):
            events.append("database")
            raise RuntimeError("database is locked")

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/cancel",
            method="POST",
        ):
            with (
                patch.object(
                    routes,
                    "TranslationProject",
                    SimpleNamespace(query=query),
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(
                    routes,
                    "reconcile_expired_active_operation",
                    return_value=operation,
                ),
                patch.object(
                    routes,
                    "_safe_operation_projection",
                    return_value={"id": operation.id, "state": "running"},
                ),
                patch.object(
                    routes,
                    "_translator_queue",
                    return_value=SimpleNamespace(request_cancel=signal_queue),
                ),
                patch.object(
                    routes,
                    "request_cancellation",
                    side_effect=fail_durable_write,
                ),
                patch.object(routes.db.session, "rollback") as rollback,
            ):
                response, status_code = routes.cancel_project_jobs.__wrapped__(
                    "project-1"
                )

        self.assertEqual(status_code, 202)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(events, ["queue", "database"])
        rollback.assert_called_once_with()

    def test_block_preview_renders_verified_complete_target_with_csp(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="document-block-v1",
            settings={},
        )
        record = SimpleNamespace(id="plan-1")
        workspace_plan = SimpleNamespace(
            record=record,
            plan=SimpleNamespace(snapshot=SimpleNamespace(resources=())),
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/preview"
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=workspace_plan,
                ),
                patch.object(
                    routes,
                    "render_persisted_target_document",
                    return_value="<html><body><h1>Cel</h1></body></html>",
                ) as render,
            ):
                response = routes.get_html_preview.__wrapped__("project-1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("sandbox", response.headers["Content-Security-Policy"])
        self.assertIn(b"Cel", response.data)
        self.assertIn(b'id="carlina-semantic-preview-policy"', response.data)
        self.assertIn(b'id="carlina-semantic-preview"', response.data)
        self.assertIn(b".pagebreak", response.data)
        render.assert_called_once_with(record)

    def test_block_preview_materializes_opaque_resources_as_standalone_html(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="document-block-v1",
            settings={"semantic_export_reference": {"provider": "doc_processor"}},
        )
        record = SimpleNamespace(id="plan-1")
        resource = object()
        workspace_plan = SimpleNamespace(
            record=record,
            plan=SimpleNamespace(snapshot=SimpleNamespace(resources=(resource,))),
        )
        final_html = (
            "<html><body><img src='.carlina-resources/res_example12345678' "
            "alt='Portrait'></body></html>"
        )
        standalone_html = (
            b"<html><body><img src='data:image/png;base64,AA==' "
            b"alt='Portrait'></body></html>"
        )
        artifact = {
            "format": "html",
            "media_type": "text/html; charset=utf-8",
            "byte_length": len(standalone_html),
            "sha256": hashlib.sha256(standalone_html).hexdigest(),
            "validation_profile": "document-processor-semantic-export",
            "validation_profile_version": "1",
        }
        export_request = SimpleNamespace(to_dict=lambda: {"contract": "request"})
        provider = SimpleNamespace(
            provider_reference=lambda: {"provider": "doc_processor"},
            client=SimpleNamespace(
                semantic_export=lambda request: {
                    "artifact": artifact,
                    "data": standalone_html,
                }
            ),
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/preview"
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=workspace_plan,
                ),
                patch.object(
                    routes,
                    "render_persisted_target_document",
                    return_value=final_html,
                ),
                patch.object(
                    routes,
                    "_semantic_export_provider",
                    return_value=provider,
                ),
                patch.object(
                    routes,
                    "build_semantic_export_request",
                    return_value=export_request,
                ) as build_request,
            ):
                response = routes.get_html_preview.__wrapped__("project-1")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"data:image/png;base64,AA==", response.data)
        self.assertIn(b'id="carlina-semantic-preview-policy"', response.data)
        self.assertIn(b'id="carlina-semantic-preview"', response.data)
        self.assertNotIn(b".carlina-resources/", response.data)
        self.assertIn("data:", response.headers["Content-Security-Policy"])
        build_request.assert_called_once()
        self.assertEqual(build_request.call_args.kwargs["export_format"], "html")
        self.assertEqual(
            build_request.call_args.kwargs["provider_witness"],
            {"provider": "doc_processor"},
        )

    def test_block_translation_freezes_selection_and_model_before_queue_delivery(self):
        app = self._app()
        settings = {
            "execution_mode": "managed",
            "translator_model": "provider/translator",
            "analyzer_model": "provider/analyzer",
            "researcher_model": "",
            "enable_pre_analysis": True,
            "enable_research": False,
        }
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings=settings,
        )
        source_plan = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        workspace_plan = SimpleNamespace(
            record=source_plan,
            plan=SimpleNamespace(
                blocks=(SimpleNamespace(id="block-1"), SimpleNamespace(id="block-2"))
            ),
        )
        owner = object()
        execution_profile = object()
        window_profile = SimpleNamespace(
            to_dict=lambda: {
                "profile_id": "document-block-window",
                "profile_version": "1",
                "capacity_version": "c" * 64,
                "max_blocks": 4,
                "max_input_tokens": 32_000,
                "output_reserve_tokens": 4_096,
            }
        )
        operation = self._operation()
        operation.kind = "document_block_translate"
        frozen_config = {"contract_version": "document-block-workflow-config-v1"}
        published = {}

        def publish(_project, **kwargs):
            published.update(kwargs)
            kwargs["initialize_operation"](operation, True)
            return operation, None

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/translate_all",
            method="POST",
            json={
                "scope": {
                    "mode": "explicit",
                    "block_ids": ["block-2", "block-1"],
                },
                "translator_model": "provider/alternate",
                "translation_reasoning": "high",
            },
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes, "_validate_block_model_settings"
                ) as validate_models,
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=workspace_plan,
                ),
                patch.object(
                    routes,
                    "resolve_execution_profile",
                    return_value=execution_profile,
                ) as resolve_profile,
                patch.object(
                    routes,
                    "derive_window_profile",
                    return_value=window_profile,
                ) as derive_profile,
                patch.object(routes, "_frozen_block_research", return_value=None),
                patch.object(
                    routes,
                    "snapshot_protected_targets",
                    return_value={
                        "contract_version": "translator-protected-targets-v1",
                        "targets": [],
                    },
                ) as protected,
                patch.object(routes.db.session, "get", return_value=owner),
                patch.object(
                    routes,
                    "freeze_block_workflow_config",
                    return_value=frozen_config,
                ) as freeze,
                patch.object(routes, "persist_block_workflow_config") as persist,
                patch.object(routes, "_publish_operation", side_effect=publish),
            ):
                response, status_code = routes.trigger_translation.__wrapped__(
                    "project-1"
                )

        self.assertEqual(status_code, 202)
        self.assertEqual(
            response.get_json()["operation"]["kind"],
            "document_block_translate",
        )
        self.assertEqual(published["kind"], "document_block_translate")
        self.assertEqual(published["handler"], "translate_document_blocks")
        self.assertEqual(published["source_plan_id"], "plan-1")
        self.assertEqual(published["source_plan_hash"], "b" * 64)
        validate_models.assert_called_once_with(
            {**settings, "translator_model": "provider/alternate"},
            user_id=7,
        )
        resolve_profile.assert_called_once_with(
            project,
            model_id="provider/alternate",
        )
        derive_profile.assert_called_once_with(
            execution_profile,
            maximum_blocks=4,
            target_input_tokens=16_000,
        )
        freeze.assert_called_once_with(
            operation,
            source_plan,
            user=owner,
            settings={**settings, "translator_model": "provider/alternate"},
            window_profile=window_profile.to_dict(),
            research=None,
            protected_targets={
                "contract_version": "translator-protected-targets-v1",
                "targets": [],
            },
            translation_scope={
                "contract_version": "translator-translation-scope-v1",
                "mode": "explicit",
                "block_ids": ["block-1", "block-2"],
            },
            translation_reasoning="high",
            context_mode="self_contained",
        )
        protected.assert_called_once_with(source_plan)
        persist.assert_called_once_with(operation, source_plan, frozen_config)

    def test_block_export_queues_only_the_pinned_semantic_provider(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            name="Example Book",
            target_lang="pl",
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings={"semantic_export_reference": {"version": 1}},
        )
        record = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        provider_reference = {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "semantic_export",
        }
        provider = SimpleNamespace(
            provider_reference=lambda: provider_reference,
        )
        operation = self._operation()
        operation.kind = "document_block_export"

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=SimpleNamespace(record=record),
                ),
                patch.object(
                    routes,
                    "_semantic_export_provider",
                    return_value=provider,
                ),
                patch(
                    "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
                    return_value=0,
                ),
                patch.object(
                    routes,
                    "_publish_operation",
                    return_value=(operation, None),
                ) as publish,
                patch("iarf_assistant.addons.translator.review_coverage.snapshot_export_review", return_value={"requirement": "none", "sha256": "c" * 64}) as review_snapshot,
            ):
                response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 202)
        self.assertEqual(response.get_json()["format"], "epub")
        publish.assert_called_once_with(
            project,
            kind="document_block_export",
            handler="export_document_blocks",
            request_payload={
                "format": "epub",
                "numbering_revision": 0,
                "export_metadata": {},
                "source_content_hash": "a" * 64,
                "source_plan_id": "plan-1",
                "source_plan_hash": "b" * 64,
                "semantic_export_provider": provider_reference,
                "epub_options": normalize_epub_options({}),
                "epub_image_mode": "jpeg",
                "review_manifest": {"requirement": "none", "sha256": "c" * 64},
            },
            queue_payload={
                "format": "epub",
                "numbering_revision": 0,
                "export_metadata": {},
                "semantic_export_provider": provider_reference,
                "epub_options": normalize_epub_options({}),
                "epub_image_mode": "jpeg",
                "review_manifest": {"requirement": "none", "sha256": "c" * 64},
            },
            idempotency_key=None,
            source_plan_id="plan-1",
            source_plan_hash="b" * 64,
        )
        review_snapshot.assert_called_once_with(project, requirement="none")

    def test_block_export_explicit_auto_uses_legacy_absent_mode_field(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            name="Example Book",
            target_lang="pl",
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings={"semantic_export_reference": {"version": 1}},
        )
        record = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        provider_reference = {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "semantic_export",
        }
        provider = SimpleNamespace(provider_reference=lambda: provider_reference)
        operation = self._operation()
        operation.kind = "document_block_export"

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
            json={"epub_image_mode": "auto"},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=SimpleNamespace(record=record),
                ),
                patch.object(routes, "_semantic_export_provider", return_value=provider),
                patch(
                    "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
                    return_value=0,
                ),
                patch.object(
                    routes, "_publish_operation", return_value=(operation, None)
                ) as publish,
                patch(
                    "iarf_assistant.addons.translator.review_coverage.snapshot_export_review",
                    return_value={"requirement": "none", "sha256": "c" * 64},
                ),
            ):
                _response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 202)
        payload = publish.call_args.kwargs
        self.assertNotIn("epub_image_mode", payload["request_payload"])
        self.assertNotIn("epub_image_mode", payload["queue_payload"])

    def test_block_export_merges_saved_metadata_with_request_overrides(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            name="Example Book",
            target_lang="en",
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings={
                "semantic_export_reference": {"version": 1},
                "export_metadata": {
                    "title": "Saved title",
                    "authors": ["Saved author"],
                    "translators": ["Saved translator"],
                },
            },
        )
        record = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        provider_reference = {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "semantic_export",
        }
        provider = SimpleNamespace(provider_reference=lambda: provider_reference)
        operation = self._operation()
        operation.kind = "document_block_export"

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
            json={"export_metadata": {"title": "  Request title  "}, "epub_image_mode": "jpeg"},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=SimpleNamespace(record=record),
                ),
                patch.object(
                    routes,
                    "_semantic_export_provider",
                    return_value=provider,
                ),
                patch(
                    "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
                    return_value=0,
                ),
                patch.object(
                    routes,
                    "_publish_operation",
                    return_value=(operation, None),
                ) as publish,
                patch("iarf_assistant.addons.translator.review_coverage.snapshot_export_review", return_value={"requirement": "none", "sha256": "c" * 64}),
            ):
                _response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 202)
        payload = publish.call_args.kwargs
        expected = {
            "title": "Request title",
            "authors": ["Saved author"],
            "translators": ["Saved translator"],
        }
        self.assertEqual(payload["request_payload"]["export_metadata"], expected)
        self.assertEqual(payload["queue_payload"]["export_metadata"], expected)
        self.assertEqual(payload["request_payload"]["epub_image_mode"], "jpeg")
        self.assertEqual(payload["queue_payload"]["epub_image_mode"], "jpeg")
        self.assertEqual(
            payload["request_payload"]["epub_options"], normalize_epub_options({})
        )
        self.assertEqual(
            payload["queue_payload"]["epub_options"], normalize_epub_options({})
        )

    def test_block_export_retry_without_options_reuses_the_frozen_profile(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            target_lang="en",
            engine_contract="document-block-v1",
            source_content_hash="a" * 64,
            settings={"semantic_export_reference": {"version": 1}},
        )
        record = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        provider_reference = {
            "version": 1,
            "capability": "document.semantic_export",
            "provider_addon_id": "doc_processor",
            "provider_capability_id": "semantic_export",
        }
        provider = SimpleNamespace(provider_reference=lambda: provider_reference)
        operation = self._operation()
        operation.kind = "document_block_export"
        frozen_options = normalize_epub_options(
            {"direction": "rtl", "group_endnotes": False}
        )
        review_hash = "d" * 64
        operation.checkpoint_json = {
            "epub_options": frozen_options,
            "epub_review_hash": review_hash,
            "epub_image_mode": "jpeg",
        }
        operation.result_json = {}
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(
                one_or_none=lambda: operation
            )
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
            headers={"Idempotency-Key": "frozen-epub-retry"},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "TranslationOperation", SimpleNamespace(query=query)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(
                    routes,
                    "load_block_source_plan",
                    return_value=SimpleNamespace(record=record),
                ),
                patch.object(routes, "_semantic_export_provider", return_value=provider),
                patch(
                    "iarf_assistant.addons.translator.numbering_sync.numbering_revision",
                    return_value=0,
                ),
                patch.object(routes, "_publish_operation", return_value=(operation, None)) as publish,
            ):
                _response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 202)
        self.assertEqual(publish.call_args.kwargs["request_payload"]["epub_image_mode"], "jpeg")
        self.assertEqual(publish.call_args.kwargs["queue_payload"]["epub_image_mode"], "jpeg")
        self.assertEqual(
            publish.call_args.kwargs["request_payload"]["epub_options"],
            frozen_options,
        )
        self.assertEqual(
            publish.call_args.kwargs["queue_payload"]["epub_options"],
            frozen_options,
        )
        self.assertEqual(
            publish.call_args.kwargs["request_payload"]["epub_review_hash"],
            review_hash,
        )
        self.assertEqual(
            publish.call_args.kwargs["queue_payload"]["epub_review_hash"],
            review_hash,
        )

    def test_block_export_rejects_invalid_metadata_before_publishing(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            target_lang="en",
            engine_contract="document-block-v1",
            settings={},
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
            json={"export_metadata": {"unknown": "value"}},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(routes, "_publish_operation") as publish,
            ):
                response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 400)
        self.assertEqual(response.get_json()["code"], "export-metadata-key")
        publish.assert_not_called()

    def test_block_export_rejects_unknown_top_level_body_keys(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            target_lang="en",
            engine_contract="document-block-v1",
            settings={},
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=epub",
            method="POST",
            json={"title": "Unexpected envelope field"},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(routes, "_publish_operation") as publish,
            ):
                response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 400)
        self.assertEqual(response.get_json()["code"], "export-metadata-key")
        publish.assert_not_called()

    def test_block_export_rejects_epub_options_for_non_epub(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            target_lang="en",
            engine_contract="document-block-v1",
            settings={},
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=docx",
            method="POST",
            json={"epub_options": {}},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(routes, "_publish_operation") as publish,
            ):
                response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 400)
        self.assertEqual(
            response.get_json()["code"], "semantic-export-epub-options-format"
        )
        publish.assert_not_called()

    def test_block_export_rejects_epub_review_hash_for_non_epub(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            target_lang="en",
            engine_contract="document-block-v1",
            settings={},
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=docx",
            method="POST",
            json={"epub_review_hash": "a" * 64},
        ):
            g.project = project
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "is_document_block_engine", return_value=True),
                patch.object(routes, "_publish_operation") as publish,
            ):
                response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 400)
        self.assertEqual(response.get_json()["code"], "epub-review-hash-format")
        publish.assert_not_called()

    def test_semantic_export_provider_uses_its_independent_pinned_requirement(self):
        settings = {
            "semantic_export_reference": {
                "version": 1,
                "capability": "document.semantic_export",
                "provider_addon_id": "doc_processor",
                "provider_capability_id": "semantic_export",
            }
        }
        provider = object()
        addon_context = object()

        with (
            patch.object(
                routes.blueprint,
                "addon_context",
                addon_context,
                create=True,
            ),
            patch.object(
                routes, "resolve_capability_provider", return_value=provider
            ) as resolve,
        ):
            assert routes._semantic_export_provider(7, settings) is provider

        resolve.assert_called_once_with(
            addon_context,
            requirement_id="semantic_export",
            capability="document.semantic_export",
            user_id=7,
            settings=settings,
            setting_key="semantic_export_reference",
        )

    def test_project_job_status_hides_worker_file_paths(self):
        app = self._app()
        fake_queue = FakeQueue(
            {
                "status": "completed",
                "result": {
                    "kind": "file_download",
                    "output_path": "C:/temp/private-output.docx",
                    "download_name": "book.docx",
                    "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "user_id": 7,
                    "project_id": "project-1",
                },
            }
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/jobs/job-123"
        ):
            g.project = SimpleNamespace(id="project-1", user_id=7)
            g.is_guest = True
            with patch.object(routes, "_translator_queue", return_value=fake_queue):
                with patch.object(
                    routes.TranslationOperation,
                    "query",
                    self._operation_query(self._operation()),
                ):
                    response, status_code = routes.get_project_job_status.__wrapped__(
                        "project-1", "job-123"
                    )

        self.assertEqual(status_code, 403)
        self.assertIn("cannot access", response.get_json()["error"])

    def test_export_status_uses_durable_failure_without_private_details(self):
        for state in ("failed", "cancelled", "partial"):
            with self.subTest(state=state):
                payload = routes._public_job_status(
                    "job-1",
                    {"status": "queued", "error": "PRIVATE_TRACE", "progress": 25},
                    project_id="project-1",
                    operation=SimpleNamespace(
                        state=state, kind="document_block_export", error_detail="PRIVATE_DIAGNOSTIC"
                    ),
                )
                self.assertEqual(payload["status"], state)
                self.assertIn("export", payload["message"])
                self.assertNotIn("PRIVATE", str(payload))
                self.assertNotIn("result", payload)

    def test_export_completion_waits_for_delivery_artifact(self):
        payload = routes._public_job_status(
            "job-1", {"status": "running", "progress": 100},
            operation=SimpleNamespace(state="completed", kind="document_block_export"),
        )
        self.assertEqual(payload["status"], "running")
        self.assertNotIn("result", payload)

    def test_aligned_source_splitter_is_strict_and_owner_scoped(self):
        settings = {"translation_unit_mode": "aligned_sentences"}
        reference = {"capability": "text.segmentation", "provider_addon_id": "sentence_segmenter"}
        split = Mock(return_value=["משפט אחד.", "משפט שני."])
        provider = SimpleNamespace(
            client=SimpleNamespace(split_text=split), provider_reference=lambda: reference
        )
        with (
            patch.object(routes.blueprint, "addon_context", object(), create=True),
            patch.object(routes, "resolve_capability_provider", return_value=provider) as resolve,
        ):
            callback = routes._source_sentence_splitter(7, settings)
        self.assertEqual(resolve.call_args.kwargs["user_id"], 7)
        self.assertEqual(resolve.call_args.kwargs["capability"], "text.segmentation")
        self.assertEqual(settings["sentence_segmenter_reference"], reference)
        self.assertEqual(callback("משפט אחד. משפט שני."), ["משפט אחד.", "משפט שני."])
        split.assert_called_once_with("משפט אחד. משפט שני.", raise_on_failure=True)

    def test_block_mode_does_not_require_a_sentence_segmenter(self):
        with patch.object(routes, "resolve_capability_provider") as resolve:
            self.assertIsNone(routes._source_sentence_splitter(7, {"translation_unit_mode": "block"}))
        resolve.assert_not_called()

    def test_aligned_source_splitter_reports_unavailability_safely(self):
        with (
            patch.object(
                routes, "resolve_capability_provider", side_effect=RuntimeError("PRIVATE_DETAIL")
            ),
            self.assertRaises(routes.SourceUnavailableError) as caught,
        ):
            routes._source_sentence_splitter(7, {"translation_unit_mode": "aligned_sentences"})
        self.assertIn("Sentence Segmenter", str(caught.exception))
        self.assertNotIn("PRIVATE_DETAIL", str(caught.exception))

    def test_project_job_download_rejects_mismatched_project_result(self):
        app = self._app()
        fake_queue = FakeQueue(
            {
                "status": "completed",
                "result": {
                    "kind": "file_download",
                    "output_path": "C:/temp/private-output.docx",
                    "download_name": "book.docx",
                    "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "user_id": 7,
                    "project_id": "other-project",
                },
            }
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/jobs/job-123/download"
        ):
            g.project = SimpleNamespace(id="project-1", user_id=7)
            g.is_guest = True
            with patch.object(routes, "_translator_queue", return_value=fake_queue):
                with patch.object(
                    routes.TranslationOperation,
                    "query",
                    self._operation_query(self._operation()),
                ):
                    response, status_code = (
                        routes.download_project_job_result.__wrapped__(
                            "project-1", "job-123"
                        )
                    )

        self.assertEqual(status_code, 403)
        self.assertIn("cannot download", response.get_json()["error"])

    def test_guest_cannot_trigger_translation_jobs(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="segment-v1",
            settings={"source_type": "subtitle_studio"},
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/translate_all", method="POST"
        ):
            g.project = project
            g.is_guest = True
            response, status_code = routes.trigger_translation.__wrapped__("project-1")

        self.assertEqual(status_code, 403)
        self.assertIn(
            "Guests cannot start AI translation jobs", response.get_json()["error"]
        )

    def test_guest_cannot_trigger_terminology_analysis(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="segment-v1",
            settings={"source_type": "subtitle_studio"},
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/analyze", method="POST"
        ):
            g.project = project
            g.is_guest = True
            response, status_code = routes.trigger_analysis.__wrapped__("project-1")

        self.assertEqual(status_code, 403)
        self.assertIn(
            "Guests cannot run terminology analysis", response.get_json()["error"]
        )

    def test_guest_can_only_export_html(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            name="Episode",
            target_lang="pl",
            engine_contract="segment-v1",
            settings={"source_type": "subtitle_studio"},
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=docx", method="GET"
        ):
            g.project = project
            g.is_guest = True
            response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 403)
        self.assertIn(
            "Guests can only export HTML previews", response.get_json()["error"]
        )

    def test_pinned_document_serializes_stable_source_label_without_provider_read(self):
        app = self._app()
        captured = datetime.now()

        class Project:
            id = "project-1"
            user_id = 7
            engine_contract = "document-block-v1"
            document_id = "document-1"
            source_snapshot = "<p>Private source</p>"
            source_content_hash = "a" * 64
            source_captured_at = captured
            operations = []
            settings = {
                "source_type": "document",
                "source_snapshot_label": "Pinned book.html",
            }

            def to_dict(self):
                return {
                    "id": self.id,
                    "settings": self.settings,
                    "share_token": "secret",
                }

        with app.test_request_context("/addons/translator/api/projects/project-1"):
            g.is_guest = False
            payload = routes._serialize_project(Project())

        self.assertEqual(
            payload["document"],
            {
                "id": "document-1",
                "filename": "Pinned book.html",
                "status": "snapshotted",
            },
        )
        self.assertNotIn("<p>Private source</p>", str(payload))

    def test_guest_serializer_redacts_owner_only_settings(self):
        app = self._app()

        class Project:
            id = "project-1"
            user_id = 7
            engine_contract = "document-block-v1"
            document_id = "document-1"
            source_snapshot = "<p>Private source</p>"
            source_content_hash = "a" * 64
            source_captured_at = datetime.now()
            operations = []
            settings = {
                "source_type": "document",
                "custom_prompt": "PROMPT_SENTINEL",
                "rag_collection_ids": ["COLLECTION_SENTINEL"],
                "research_plugin_ids": ["provider.secret"],
                "translator_model": "MODEL_SENTINEL",
                "source_reference": {"token": "SOURCE_SENTINEL"},
                "source_snapshot_provenance": {"path": "PROVENANCE_SENTINEL"},
                "subtitle": {"units": {"cue": "UNIT_SENTINEL"}},
            }

            def to_dict(self):
                return {
                    "id": self.id,
                    "settings": self.settings,
                    "share_token": "SHARE_SENTINEL",
                }

        with app.test_request_context(
            "/addons/translator/api/projects/project-1?token=x"
        ):
            g.is_guest = True
            payload = routes._serialize_project(Project())

        self.assertEqual(payload["settings"], {"source_type": "document"})
        serialized = str(payload)
        for sentinel in (
            "PROMPT_SENTINEL",
            "COLLECTION_SENTINEL",
            "SOURCE_SENTINEL",
            "PROVENANCE_SENTINEL",
            "UNIT_SENTINEL",
            "MODEL_SENTINEL",
            "SHARE_SENTINEL",
        ):
            self.assertNotIn(sentinel, serialized)

    def test_guest_subtitle_serializer_uses_snapshot_without_provider_metadata(self):
        app = self._app()

        class Project:
            id = "project-1"
            user_id = 7
            engine_contract = "segment-v1"
            document_id = None
            source_snapshot = '{"private":"subtitle source"}'
            source_content_hash = "a" * 64
            source_captured_at = datetime.now()
            operations = []
            settings = {
                "source_type": "subtitle_studio",
                "subtitle_project_id": 33,
                "subtitle": {
                    "source_project_name": "Private episode",
                    "source_media_filename": "private.wav",
                },
            }

            def to_dict(self):
                return {"id": self.id, "settings": self.settings, "share_token": "x"}

        with app.test_request_context(
            "/addons/translator/api/projects/project-1?token=x"
        ):
            g.is_guest = True
            with patch.object(
                routes,
                "_subtitle_source_provider",
                side_effect=AssertionError("guest must not call provider"),
            ):
                payload = routes._serialize_project(Project())
        self.assertEqual(
            payload["subtitle_project"]["name"], "Snapshotted subtitle source"
        )
        self.assertNotIn("Private episode", str(payload))
        self.assertNotIn("private.wav", str(payload))

    def test_saved_unavailable_research_ids_can_be_disabled(self):
        app = self._app()
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/settings"
        ):
            disabled = {
                "enable_research": False,
                "research_plugin_ids": ["web.research"],
            }
            routes._validate_research_plugin_ids(disabled)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                routes._validate_research_plugin_ids(
                    {"enable_research": True, "research_plugin_ids": ["web.research"]}
                )

    def test_block_research_requires_preliminary_analysis(self):
        with (
            patch.object(routes, "_validate_chat_phase_model_settings"),
            self.assertRaisesRegex(ValueError, "requires preliminary"),
        ):
            routes._validate_block_model_settings(
                {
                    "enable_pre_analysis": False,
                    "enable_research": True,
                    "research_tool_selections": [],
                },
                user_id=7,
            )

    def test_block_settings_default_and_validate_translation_batch_budget(self):
        with patch.object(routes, "_validate_chat_phase_model_settings"):
            settings = {
                "enable_research": False,
                "enable_translator_notes": True,
            }
            routes._validate_block_model_settings(settings, user_id=7)
            self.assertEqual(settings["translation_batch_input_tokens"], 16_000)
            self.assertEqual(settings["translation_batch_max_blocks"], 4)
            self.assertIs(settings["enable_translator_notes"], True)

            small_settings = {
                "enable_research": False,
                "translation_batch_input_tokens": 2_048,
            }
            routes._validate_block_model_settings(small_settings, user_id=7)
            self.assertEqual(small_settings["translation_batch_input_tokens"], 2_048)

            bounded_settings = {
                "enable_research": False,
                "translation_batch_max_blocks": 18,
            }
            routes._validate_block_model_settings(bounded_settings, user_id=7)
            self.assertEqual(bounded_settings["translation_batch_max_blocks"], 18)

            with self.assertRaisesRegex(ValueError, "between 2,048 and 1,000,000"):
                routes._validate_block_model_settings(
                    {
                        "enable_research": False,
                        "translation_batch_input_tokens": 1_024,
                    },
                    user_id=7,
                )

            with self.assertRaisesRegex(ValueError, "between 1 and 256"):
                routes._validate_block_model_settings(
                    {
                        "enable_research": False,
                        "translation_batch_max_blocks": 257,
                    },
                    user_id=7,
                )

            for invalid in (True, 24.5):
                with self.subTest(invalid_block_limit=invalid):
                    with self.assertRaisesRegex(ValueError, "whole number"):
                        routes._validate_block_model_settings(
                            {
                                "enable_research": False,
                                "translation_batch_max_blocks": invalid,
                            },
                            user_id=7,
                        )

    def test_passive_block_settings_require_no_managed_model(self):
        settings = {
            "execution_mode": "passive",
            "translator_model": "stale/managed-model",
            "enable_pre_analysis": False,
            "enable_research": False,
            "annotation_profile": "study",
        }
        with patch.object(
            routes,
            "_validate_chat_phase_model_settings",
            side_effect=AssertionError("passive settings must not resolve a model"),
        ):
            routes._validate_block_model_settings(settings, user_id=7)

        self.assertEqual(settings["execution_mode"], "passive")
        self.assertEqual(settings["translator_model"], "")
        self.assertEqual(settings["analyzer_model"], "")
        self.assertEqual(settings["researcher_model"], "")
        self.assertEqual(settings["annotation_profile"], "study")
        self.assertIs(settings["enable_translator_notes"], True)

        for field in ("enable_pre_analysis", "enable_research"):
            invalid = dict(settings)
            invalid[field] = True
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "Passive"
            ):
                routes._validate_block_model_settings(invalid, user_id=7)

    def test_project_settings_rejects_non_object_json(self):
        app = self._app()
        project = SimpleNamespace(id="project-1", settings={})
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(first_or_404=lambda: project)
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/settings",
            method="PUT",
            json=["not", "an", "object"],
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes.TranslationProject, "query", query),
                patch.object(
                    routes, "reconcile_expired_active_operation", return_value=None
                ),
            ):
                response, status_code = routes.manage_project_settings.__wrapped__(
                    "project-1"
                )
        self.assertEqual(status_code, 400)
        self.assertIn("JSON object", response.get_json()["error"])

    def test_sentence_mode_switch_preserves_legacy_source_plan(self):
        app = self._app()
        for current_mode, profile, expected_status in (
            ("block", "unsupported-language", 409),
            ("block", "sentence-segmenter", 200),
            ("aligned_sentences", "unsupported-language", 200),
        ):
            project = SimpleNamespace(
                id="project-1",
                engine_contract="document-block-v1",
                settings={"translation_unit_mode": current_mode},
            )
            query = SimpleNamespace(
                filter_by=lambda _project=project, **_kwargs: SimpleNamespace(
                    first_or_404=lambda: _project
                )
            )
            source_plan = SimpleNamespace(
                plan=SimpleNamespace(
                    blocks=[SimpleNamespace(anchors=[SimpleNamespace(splitter_profile=profile)])]
                )
            )
            with (
                self.subTest(current_mode=current_mode, profile=profile),
                app.test_request_context(
                    "/addons/translator/api/projects/project-1/settings",
                    method="PUT",
                    json={"translation_unit_mode": "aligned_sentences"},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes.TranslationProject, "query", query),
                patch.object(routes, "reconcile_expired_active_operation", return_value=None),
                patch.object(routes, "_validate_block_model_settings"),
                patch.object(routes, "load_block_source_plan", return_value=source_plan) as load,
                patch.object(routes, "flag_modified"),
                patch.object(routes.db.session, "commit") as commit,
            ):
                result = routes.manage_project_settings.__wrapped__("project-1")
                response, status = result if isinstance(result, tuple) else (result, 200)
                self.assertEqual(status, expected_status)
                if expected_status == 409:
                    self.assertIn("Create a new sentence-mode project", response.get_json()["error"])
                    self.assertEqual(project.settings, {"translation_unit_mode": "block"})
                    commit.assert_not_called()
                else:
                    commit.assert_called_once()
                if current_mode == "aligned_sentences":
                    load.assert_not_called()
                else:
                    load.assert_called_once_with(project)

    def test_guest_context_and_workspace_state_redact_rag_collections(self):
        app = self._app()

        class Query:
            def filter_by(self, **_kwargs):
                return self

            def filter(self, *_args):
                return self

            def order_by(self, *_args):
                return self

            def count(self):
                return 0

            def offset(self, _offset):
                return self

            def limit(self, _limit):
                return self

            def all(self):
                return []

        project = SimpleNamespace(
            id="project-1",
            working_memory="Private memory",
            engine_contract="segment-v1",
            settings={
                "source_type": "subtitle_studio",
                "rag_collection_ids": ["private-collection"],
            },
        )
        query = Query()
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/context"
        ):
            g.project = project
            g.is_guest = True
            with patch.object(routes.GlossaryTerm, "query", query):
                response = routes.manage_context.__wrapped__("project-1")
        self.assertEqual(response.get_json()["rag_collection_ids"], [])

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/workspace_state?limit=80"
        ):
            g.project = project
            g.is_guest = True
            with (
                patch.object(routes.GlossaryTerm, "query", query),
                patch.object(routes.TranslationSegment, "query", query),
                patch.object(
                    routes, "_serialize_project", return_value={"id": project.id}
                ),
            ):
                response = routes.get_workspace_state.__wrapped__("project-1")
        self.assertEqual(response.get_json()["context"]["rag_collection_ids"], [])

    def test_guest_project_metrics_are_forbidden(self):
        app = self._app()
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/metrics"
        ):
            g.project = SimpleNamespace(id="project-1")
            g.is_guest = True
            response, status_code = routes.get_project_metrics.__wrapped__("project-1")
        self.assertEqual(status_code, 403)
        self.assertIn("cannot access project metrics", response.get_json()["error"])

    def test_single_segment_translation_requires_current_expected_version(self):
        app = self._app()

        class Segment:
            id = "seg-1"
            version = 4

            def to_dict(self):
                return {"id": self.id, "version": self.version}

        segment = Segment()
        query = SimpleNamespace(
            filter_by=lambda **_kwargs: SimpleNamespace(first_or_404=lambda: segment)
        )
        project = SimpleNamespace(
            id="project-1",
            engine_contract="segment-v1",
            settings={"source_type": "subtitle_studio"},
        )
        with app.test_request_context(
            "/addons/translator/api/projects/project-1/segments/seg-1/translate",
            method="POST",
            json={"expected_version": 3},
        ):
            g.project = project
            g.is_guest = False
            with patch.object(routes.TranslationSegment, "query", query):
                response, status_code = routes.translate_single_segment.__wrapped__(
                    "project-1", "seg-1"
                )
        self.assertEqual(status_code, 409)
        self.assertEqual(response.get_json()["latest_segment"]["version"], 4)

    def test_create_project_envelope_rejects_malformed_or_oversized_values(self):
        valid = {
            "name": "Book",
            "source_lang": "English",
            "target_lang": "Polish",
            "source_type": "document",
            "document_id": "doc-1",
        }
        self.assertEqual(
            routes._validate_create_project_request(valid),
            ("Book", "en", "pl", "document", "doc-1"),
        )
        self.assertEqual(
            routes._validate_create_project_request({**valid, "source_lang": "auto"}),
            ("Book", "Auto-detect", "pl", "document", "doc-1"),
        )
        invalid_cases = (
            ({**valid, "name": ["not text"]}, "name must be text"),
            ({**valid, "target_lang": " "}, "target_lang is required"),
            (
                {**valid, "target_lang": "Klingon"},
                "target_lang must be a language name or a BCP 47 tag",
            ),
            (
                {**valid, "source_lang": "Klingon"},
                "source_lang must be Auto-detect, a language name, or a BCP 47 tag",
            ),
            ({**valid, "source_lang": "x" * 51}, "source_lang must be at most"),
            ({**valid, "source_type": "text"}, "source_type must be document"),
            ({**valid, "document_id": "x" * 37}, "document_id must be at most"),
            (
                {**valid, "source_provider_addon_id": "x" * 129},
                "source_provider_addon_id must be at most",
            ),
        )
        for payload, message in invalid_cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, message):
                    routes._validate_create_project_request(payload)

    def test_engine_selection_is_block_only_for_documents_and_segments_for_subtitles(
        self,
    ):
        document = {"source_type": "document"}
        self.assertEqual(
            routes._requested_engine_contract(document, "document"),
            "document-block-v1",
        )
        self.assertEqual(
            routes._requested_engine_contract(
                {"source_type": "document", "engine_contract": "document-block-v1"},
                "document",
            ),
            "document-block-v1",
        )
        self.assertEqual(
            routes._requested_engine_contract(
                {
                    "source_type": "subtitle_studio",
                    "engine_contract": "document-block-v1",
                },
                "subtitle_studio",
            ),
            "segment-v1",
        )
        with self.assertRaisesRegex(ValueError, "requires engine_contract"):
            routes._requested_engine_contract(
                {"source_type": "document", "engine_contract": "segment-v1"},
                "document",
            )
        with self.assertRaisesRegex(ValueError, "requires engine_contract"):
            routes._requested_engine_contract(
                {"source_type": "document", "engine_contract": "unknown"},
                "document",
            )
        with self.assertRaisesRegex(ValueError, "requires engine_contract"):
            routes._requested_engine_contract(
                {"source_type": "document", "engine_contract": "   "},
                "document",
            )

    def test_strict_document_snapshot_preserves_provider_contract_and_rejects_legacy(
        self,
    ):
        html = "<main><h1>Title</h1><p>Source.</p></main>"
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
        strict = {
            "id": "doc-1",
            "document_id": "doc-1",
            "snapshot_contract_version": "document-snapshot-v1",
            "status": "completed",
            "output_html": html,
            "content_hash": content_hash,
            "semantic_contract_version": "1",
            "semantic_contract_mode": "completed",
            "semantic_content_hash": content_hash,
            "filename": "book.html",
            "captured_at": "2026-08-26T12:00:00+00:00",
            "source_updated_at": "2026-08-25T12:00:00+00:00",
            "metadata": {"title": "Title"},
            "provenance": {"knowledge_source_id": "source-1"},
        }
        provider = SimpleNamespace(
            client=SimpleNamespace(get_document_snapshot=lambda _document_id: strict)
        )
        result = routes._document_snapshot(provider, "doc-1", strict=True)
        self.assertEqual(result["metadata"], {"title": "Title"})
        self.assertEqual(result["semantic_contract_mode"], "completed")
        self.assertEqual(result["semantic_content_hash"], content_hash)

        legacy = dict(strict)
        legacy.pop("semantic_contract_mode")
        legacy.pop("semantic_content_hash")
        legacy_provider = SimpleNamespace(
            client=SimpleNamespace(get_document_snapshot=lambda _document_id: legacy)
        )
        with self.assertRaisesRegex(ValueError, "strict semantic snapshot"):
            routes._document_snapshot(legacy_provider, "doc-1", strict=True)

    def test_publish_operation_passes_exact_block_source_pins(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1", user_id=7, source_content_hash="a" * 64
        )
        operation = self._operation(project_id=project.id)
        operation.state = "completed"
        with (
            app.test_request_context("/addons/translator/api/projects/project-1"),
            patch.object(
                routes,
                "create_operation",
                return_value=(operation, False),
            ) as create,
            patch.object(routes.db.session, "commit"),
        ):
            result, error = routes._publish_operation(
                project,
                kind="plan_document_blocks",
                handler="plan_document_blocks",
                request_payload={"source_plan_id": "plan-1"},
                source_plan_id="plan-1",
                source_plan_hash="b" * 64,
            )
        self.assertIs(result, operation)
        self.assertIsNone(error)
        self.assertEqual(create.call_args.kwargs["source_plan_id"], "plan-1")
        self.assertEqual(create.call_args.kwargs["source_plan_hash"], "b" * 64)

    def test_publish_operation_initializes_frozen_evidence_before_commit(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1", user_id=7, source_content_hash="a" * 64
        )
        operation = self._operation(project_id=project.id)
        operation.state = "completed"
        order = []

        def initialize(received, created):
            self.assertIs(received, operation)
            self.assertFalse(created)
            order.append("initialize")

        with (
            app.test_request_context("/addons/translator/api/projects/project-1"),
            patch.object(
                routes,
                "create_operation",
                return_value=(operation, False),
            ),
            patch.object(
                routes.db.session,
                "commit",
                side_effect=lambda: order.append("commit"),
            ),
        ):
            result, error = routes._publish_operation(
                project,
                kind="translate_document_blocks",
                handler="translate_document_blocks",
                request_payload={"source_plan_id": "plan-1"},
                source_plan_id="plan-1",
                source_plan_hash="b" * 64,
                initialize_operation=initialize,
            )

        self.assertIs(result, operation)
        self.assertIsNone(error)
        self.assertEqual(order, ["initialize", "commit"])

    def test_publish_operation_rolls_back_failed_initializer_without_delivery(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1", user_id=7, source_content_hash="a" * 64
        )
        operation = self._operation(project_id=project.id)

        def fail_initializer(*_args):
            raise RuntimeError("freeze failed")

        with (
            app.test_request_context("/addons/translator/api/projects/project-1"),
            patch.object(
                routes,
                "create_operation",
                return_value=(operation, True),
            ),
            patch.object(routes.db.session, "commit") as commit,
            patch.object(routes.db.session, "rollback") as rollback,
            patch.object(routes, "_deliver_queued_operation") as deliver,
            self.assertRaisesRegex(RuntimeError, "freeze failed"),
        ):
            routes._publish_operation(
                project,
                kind="translate_document_blocks",
                handler="translate_document_blocks",
                request_payload={"source_plan_id": "plan-1"},
                initialize_operation=fail_initializer,
            )

        rollback.assert_called_once()
        commit.assert_not_called()
        deliver.assert_not_called()

    def test_document_snapshot_rejects_html_larger_than_20_mib(self):
        oversized_html = "x" * (MAX_DOCUMENT_SNAPSHOT_BYTES + 1)
        provider = SimpleNamespace(
            client=SimpleNamespace(
                get_document_snapshot=lambda _document_id: {
                    "id": "doc-1",
                    "status": "completed",
                    "output_html": oversized_html,
                    "content_hash": hashlib.sha256(
                        oversized_html.encode("utf-8")
                    ).hexdigest(),
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "20 MiB"):
            routes._document_snapshot(provider, "doc-1")

    def test_create_project_unexpected_error_is_generic(self):
        app = self._app()
        source_html = "<main><p>Source</p></main>"
        source_hash = hashlib.sha256(source_html.encode("utf-8")).hexdigest()
        snapshot = {
            "id": "doc-1",
            "document_id": "doc-1",
            "snapshot_contract_version": "document-snapshot-v1",
            "status": "completed",
            "output_html": source_html,
            "content_hash": source_hash,
            "semantic_contract_version": "1",
            "semantic_contract_mode": "completed",
            "semantic_content_hash": source_hash,
            "captured_at": "2026-09-02T12:00:00+00:00",
            "source_updated_at": "2026-09-02T11:59:00+00:00",
            "metadata": {},
            "provenance": {},
        }
        provider = SimpleNamespace(
            client=SimpleNamespace(get_document_snapshot=lambda _document_id: snapshot),
            reference=lambda **_kwargs: {"provider_addon_id": "safe"},
        )
        with app.test_request_context(
            "/addons/translator/api/projects",
            method="POST",
            json={
                "name": "Book",
                "source_lang": "English",
                "target_lang": "Polish",
                "source_type": "document",
                "document_id": "doc-1",
            },
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(
                    routes, "_document_source_provider", return_value=provider
                ),
                patch.object(routes, "_validate_block_model_settings"),
                patch.object(
                    routes,
                    "TranslationProject",
                    side_effect=RuntimeError("PRIVATE_PROVIDER_DETAIL"),
                ),
            ):
                response, status_code = routes.create_project.__wrapped__()
        self.assertEqual(status_code, 500)
        self.assertEqual(
            response.get_json()["error"], "Unable to create translation project."
        )
        self.assertNotIn("PRIVATE_PROVIDER_DETAIL", response.get_data(as_text=True))

    def test_document_create_retry_repairs_ingestion_before_returning_reuse(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            engine_contract="document-block-v1",
        )
        operation = self._operation(project_id=project.id)
        reused_response = (SimpleNamespace(get_json=lambda: {"id": project.id}), 200)
        with app.test_request_context(
            "/addons/translator/api/projects",
            method="POST",
            headers={"Idempotency-Key": "create-repair"},
            json={
                "name": "Book",
                "source_lang": "English",
                "target_lang": "Polish",
                "source_type": "document",
                "document_id": "doc-1",
            },
        ):
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes, "_validate_block_model_settings"),
                patch.object(routes, "TranslationProject", return_value=project),
                patch.object(
                    routes,
                    "create_project_idempotently",
                    return_value=(project, False),
                ),
                patch.object(
                    routes,
                    "_repair_reused_document_ingestion",
                    return_value=(operation, None),
                ) as repair,
                patch.object(
                    routes,
                    "_project_creation_reuse_response",
                    return_value=reused_response,
                ),
                patch.object(routes, "_document_source_provider") as source_provider,
            ):
                response = routes.create_project.__wrapped__()

        self.assertEqual(response, reused_response)
        repair.assert_called_once_with(project)
        source_provider.assert_not_called()

    def test_document_create_passes_sentence_splitter_and_rolls_back_invalid_partition(self):
        app = self._app()
        source_html = "<main><p>First. Second.</p></main>"
        snapshot = {
            "document_id": "doc-1",
            "output_html": source_html,
            "content_hash": hashlib.sha256(source_html.encode("utf-8")).hexdigest(),
            "provenance": {},
        }
        project = SimpleNamespace(id="project-1", user_id=7)
        provider = SimpleNamespace(reference=lambda **_kwargs: {"provider_addon_id": "safe"})

        def split_projection(_projection):
            return ["Rewritten source."]

        def reject_partition(project_arg, snapshot_arg, **kwargs):
            self.assertIs(project_arg, project)
            self.assertIs(snapshot_arg, snapshot)
            self.assertIs(kwargs["sentence_segmenter"], split_projection)
            raise routes.BlockContractError("source-sentence-segmentation-coverage")

        with (
            app.test_request_context(
                "/addons/translator/api/projects",
                method="POST",
                json={
                    "name": "Book",
                    "source_lang": "English",
                    "target_lang": "Polish",
                    "source_type": "document",
                    "document_id": "doc-1",
                    "translation_unit_mode": "aligned_sentences",
                },
            ),
            patch.object(routes, "current_user", SimpleNamespace(id=7)),
            patch.object(routes, "_validate_block_model_settings"),
            patch.object(routes, "TranslationProject", return_value=project),
            patch.object(routes, "create_project_idempotently", return_value=(project, True)),
            patch.object(routes, "_document_source_provider", return_value=provider),
            patch.object(routes, "_semantic_export_provider", return_value=None),
            patch.object(routes, "_document_snapshot", return_value=snapshot),
            patch.object(routes, "_source_sentence_splitter", return_value=split_projection) as splitter,
            patch.object(routes, "plan_and_persist_source_snapshot", side_effect=reject_partition),
            patch.object(routes, "flag_modified"),
            patch.object(routes, "_publish_operation") as publish,
            patch.object(routes.db.session, "rollback") as rollback,
        ):
            response, status = routes.create_project.__wrapped__()
        self.assertEqual(status, 400)
        self.assertEqual(response.get_json()["code"], "source_sentence_segmentation_failed")
        splitter.assert_called_once_with(7, project.settings)
        self.assertEqual(project.settings["translation_unit_mode"], "aligned_sentences")
        rollback.assert_called_once()
        publish.assert_not_called()

    def test_split_validation_requires_single_placeholder_and_faithful_partition(self):
        segment = SimpleNamespace(
            id="seg-1", original_text="First sentence.  Second sentence."
        )
        project = SimpleNamespace(skeleton_html="<p>{{seg-1}}</p>")
        routes._validate_split_partition(
            project, segment, "First sentence.", "Second sentence."
        )
        segment.original_text = "<i>First sentence. Second sentence.</i>"
        with self.assertRaisesRegex(ValueError, "inline markup cannot be split"):
            routes._validate_split_partition(
                project, segment, "<i>First sentence.", "Second sentence.</i>"
            )
        segment.original_text = "First sentence.  Second sentence."
        with self.assertRaisesRegex(ValueError, "faithfully partition"):
            routes._validate_split_partition(
                project, segment, "Rewritten first.", "Second sentence."
            )
        project.skeleton_html = "{{seg-1}} {{seg-1}}"
        with self.assertRaisesRegex(ValueError, "structure is inconsistent"):
            routes._validate_split_partition(
                project, segment, "First sentence.", "Second sentence."
            )

    def test_glossary_update_requires_boolean_review_and_unique_source_term(self):
        app = self._app()

        class Term:
            id = 1
            source_term = "hello"
            target_term = "czesc"
            context_notes = ""
            needs_review = False
            is_user_defined = False
            status = "approved"

            def to_dict(self):
                return {"id": self.id, "source_term": self.source_term}

        term = Term()
        duplicate = SimpleNamespace(id=2)

        class Query:
            def filter_by(self, **_kwargs):
                return SimpleNamespace(first_or_404=lambda: term)

            def filter(self, *_args):
                return SimpleNamespace(first=lambda: duplicate)

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/glossary/1",
            method="PUT",
            json={"needs_review": "false"},
        ):
            g.project = SimpleNamespace(id="project-1")
            g.is_guest = False
            with patch.object(routes.GlossaryTerm, "query", Query()):
                response, status_code = routes.manage_glossary_term.__wrapped__(
                    "project-1", 1
                )
        self.assertEqual(status_code, 400)
        self.assertIn("must be a boolean", response.get_json()["error"])

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/glossary/1",
            method="PUT",
            json={"source_term": "HELLO"},
        ):
            g.project = SimpleNamespace(id="project-1")
            g.is_guest = False
            with patch.object(routes.GlossaryTerm, "query", Query()):
                response, status_code = routes.manage_glossary_term.__wrapped__(
                    "project-1", 1
                )
        self.assertEqual(status_code, 409)
        self.assertIn("already exists", response.get_json()["error"])

    def test_research_provider_catalog_exposes_only_active_read_only_tools(self):
        app = self._app()
        read_tool = SimpleNamespace(
            id="web.lookup",
            addon_id="web.research",
            name="Lookup",
            function_schema={"function": {"name": "lookup", "description": "Read web"}},
            side_effects=["external_read"],
            approval_required=False,
            status=SimpleNamespace(state="active"),
        )
        write_tool = SimpleNamespace(
            id="mail.send",
            addon_id="mail",
            name="Send",
            function_schema={"function": {"name": "send", "description": "Send mail"}},
            side_effects=["external_write"],
            approval_required=False,
            status=SimpleNamespace(state="active"),
        )
        inactive_tool = SimpleNamespace(
            id="archive.lookup",
            addon_id="archive",
            name="Archive lookup",
            function_schema={"function": {"name": "lookup"}},
            side_effects=["read"],
            approval_required=False,
            status=SimpleNamespace(state="inactive"),
        )
        app.addon_manager = SimpleNamespace(
            registries=SimpleNamespace(
                tools=SimpleNamespace(
                    list=lambda: [read_tool, write_tool, inactive_tool]
                )
            ),
            plugins={
                "web.research": SimpleNamespace(
                    manifest={"name": "Web", "description": "Search"}
                ),
                "mail": SimpleNamespace(
                    manifest={"name": "Mail", "description": "Mail"}
                ),
            },
        )

        with app.test_request_context("/addons/translator/api/research-providers"):
            response = routes.get_research_providers.__wrapped__()
            payload = response.get_json()
            settings = {
                "enable_research": True,
                "research_plugin_ids": ["web.research"],
            }
            routes._validate_research_plugin_ids(settings)
            for invalid in ("../web", "web/research", "x" * 129):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    routes._validate_research_plugin_ids(
                        {"enable_research": False, "research_plugin_ids": [invalid]}
                    )
            with self.assertRaisesRegex(ValueError, "unavailable"):
                routes._validate_research_plugin_ids(
                    {"enable_research": True, "research_plugin_ids": ["mail"]}
                )

        self.assertEqual(
            payload,
            [
                {
                    "id": "web.research",
                    "name": "Web",
                    "description": "Search",
                    "tools": [
                        {
                            "id": "web.lookup",
                            "name": "lookup",
                            "description": "Read web",
                        }
                    ],
                }
            ],
        )


    def test_document_project_rejects_subtitle_export_format(self):
        app = self._app()
        project = SimpleNamespace(
            id="project-1",
            user_id=7,
            name="Book",
            target_lang="pl",
            engine_contract="document-block-v1",
            settings={"source_type": "document"},
        )

        with app.test_request_context(
            "/addons/translator/api/projects/project-1/export?format=srt", method="GET"
        ):
            g.project = project
            g.is_guest = False
            response, status_code = routes.export_project.__wrapped__("project-1")

        self.assertEqual(status_code, 400)
        self.assertEqual("Block exports support HTML, DOCX, EPUB, and PDF.", response.get_json()["error"])



    @staticmethod
    def _retranslation_project():
        return SimpleNamespace(
            id="project-1",
            user_id=7,
            source_content_hash="a" * 64,
            settings={
                "execution_mode": "managed",
                "translator_model": "translator-model",
                "analyzer_model": "analyzer-model",
                "researcher_model": "researcher-model",
                "owner_policy": "Preserve this owner policy exactly.",
                "custom_prompt": "conflicting-alias",
            },
        )

    @staticmethod
    def _retranslation_row(
        *, state="requested", choice="reuse_dossier", version=1, operation_id=None
    ):
        return SimpleNamespace(
            id="request-1",
            project_id="project-1",
            source_plan_id="plan-1",
            scope_json={
                "requested": {"kind": "blocks", "block_ids": ["block-1", "block-2"]},
                "resolved_block_ids": ["block-1", "block-2"],
            },
            custom_prompt="Apply the dossier only to this retry.",
            annotation_profile="study",
            research_choice=choice,
            dossier_revision_id="dossier-revision-1" if choice == "reuse_dossier" else None,
            rationale="",
            state=state,
            version=version,
            operation_id=operation_id,
            request_fingerprint="f" * 64,
            created_at=None,
            updated_at=None,
        )

    @staticmethod
    def _retranslation_operation(*, state="queued", operation_id="operation-1"):
        now = datetime.now()
        return SimpleNamespace(
            id=operation_id,
            project_id="project-1",
            owner_user_id=7,
            kind="document_block_translate",
            source_plan_id="plan-1",
            source_plan_hash="b" * 64,
            source_content_hash="a" * 64,
            queue_job_id=None,
            state=state,
            version=1,
            progress=0,
            message="Queued.",
            error_code=None,
            cancel_requested_at=None,
            lease_expires_at=None,
            created_at=now,
            started_at=None,
            finished_at=None,
            updated_at=now,
        )

    def _execute_retranslation_context(self, app, *, version=1):
        return app.test_request_context(
            "/addons/translator/api/projects/project-1/retranslation-requests/request-1/execute",
            method="POST",
            headers={"Idempotency-Key": "client-key"},
            json={"expected_version": version},
        )

    def test_retranslation_execute_denies_reviewer_before_operation_work(self):
        app = self._app()
        with self._execute_retranslation_context(app):
            g.access_role = "reviewer"
            with patch.object(
                routes.scholarly_review, "retranslation_request_record"
            ) as request_record:
                response, status_code = routes.execute_retranslation_request.__wrapped__(
                    "project-1", "request-1"
                )

        self.assertEqual(status_code, 403)
        self.assertEqual(response.get_json()["code"], "retranslation-owner-required")
        request_record.assert_not_called()

    def test_retranslation_execute_freezes_exact_dossier_scope_and_starts_request(self):
        app = self._app()
        project = self._retranslation_project()
        request_row = self._retranslation_row()
        source_plan = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        operation = self._retranslation_operation()
        actor = SimpleNamespace(role="owner", user_id=7)
        dossier_pin = {
            "revision_id": "dossier-revision-1",
            "record_hash": "c" * 64,
            "content_digest": "d" * 64,
            "conclusion_provenance": "managed_verified",
            "evidence_verification_state": "verified",
        }
        captured = {}

        def publish(_project, **kwargs):
            captured.update(kwargs)
            kwargs["initialize_operation"](operation, True)
            return operation, None

        def operation_response(received, *, status=202, extra=None):
            self.assertIs(received, operation)
            self.assertEqual(status, 202)
            self.assertNotIn("operation_id", extra["request"])
            return "operation-response", 202

        with self._execute_retranslation_context(app):
            g.project = project
            g.access_role = "owner"
            g.review_actor = actor
            g.is_guest = False
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes.scholarly_review, "retranslation_request_record", return_value=request_row),
                patch.object(routes.scholarly_review, "retranslation_request_projection", return_value={
                    "id": request_row.id, "scope": request_row.scope_json["requested"],
                    "resolved_block_ids": request_row.scope_json["resolved_block_ids"],
                    "custom_prompt": request_row.custom_prompt, "annotation_profile": request_row.annotation_profile,
                    "research_choice": request_row.research_choice, "dossier_revision_id": request_row.dossier_revision_id,
                    "rationale": request_row.rationale, "state": request_row.state, "version": request_row.version,
                }),
                patch.object(routes, "load_block_source_plan", return_value=SimpleNamespace(record=source_plan)),
                patch.object(routes.scholarly_review, "retranslation_dossier_pin", return_value=dossier_pin),
                patch.object(routes, "_validate_block_model_settings") as validate,
                patch.object(routes, "resolve_execution_profile", return_value=SimpleNamespace()),
                patch.object(routes, "derive_window_profile", return_value=SimpleNamespace(to_dict=lambda: {"profile": "v5"})),
                patch.object(routes, "_frozen_block_research") as frozen_research,
                patch.object(routes.db.session, "get", return_value=SimpleNamespace(id=7)),
                patch.object(routes, "snapshot_protected_targets", return_value={"frozen": True}),
                patch.object(routes, "freeze_block_workflow_config", return_value={"config": "frozen"}) as freeze,
                patch.object(routes, "persist_block_workflow_config") as persist,
                patch.object(routes.scholarly_review, "start_retranslation_request") as start,
                patch.object(routes, "_block_translation_request_payload", return_value={"base": True}),
                patch.object(routes, "_publish_operation", side_effect=publish),
                patch.object(routes, "_operation_response", side_effect=operation_response),
            ):
                response = routes.execute_retranslation_request.__wrapped__("project-1", "request-1")

        self.assertEqual(response, ("operation-response", 202))
        self.assertEqual(captured["idempotency_key"], "retranslate-request-1")
        self.assertEqual(captured["source_plan_id"], "plan-1")
        self.assertEqual(captured["source_plan_hash"], "b" * 64)
        self.assertEqual(captured["request_payload"]["retranslation"], {
            "request_id": "request-1", "request_fingerprint": "f" * 64,
            "request_version": 1, "research_choice": "reuse_dossier",
            "dossier_revision_id": "dossier-revision-1", "dossier_record_hash": "c" * 64,
        })
        settings = validate.call_args.args[0]
        self.assertEqual(settings["annotation_profile"], "study")
        self.assertTrue(settings["enable_translator_notes"])
        self.assertFalse(settings["enable_pre_analysis"])
        self.assertFalse(settings["enable_research"])
        self.assertNotIn("custom_prompt", settings)
        self.assertEqual(settings["owner_policy"], "Preserve this owner policy exactly.\n\nRetranslation request addendum:\nApply the dossier only to this retry.")
        frozen_research.assert_not_called()
        self.assertEqual(freeze.call_args.kwargs["translation_scope"], {
            "contract_version": routes.TRANSLATION_SCOPE_CONTRACT_VERSION,
            "mode": "explicit", "block_ids": ["block-1", "block-2"],
        })
        self.assertEqual(freeze.call_args.kwargs["research_dossier"], dossier_pin)
        persist.assert_called_once_with(operation, source_plan, {"config": "frozen"})
        start.assert_called_once_with(
            request_row, expected_version=1, operation=operation, actor=actor
        )

    def test_retranslation_execute_rejects_awaiting_external_request_before_publish(self):
        app = self._app()
        project = self._retranslation_project()
        request_row = self._retranslation_row(choice="await_external")
        with self._execute_retranslation_context(app):
            g.project = project
            g.access_role = "owner"
            g.review_actor = SimpleNamespace(role="owner", user_id=7)
            with (
                patch.object(routes.scholarly_review, "retranslation_request_record", return_value=request_row),
                patch.object(routes, "load_block_source_plan", return_value=SimpleNamespace(record=SimpleNamespace(id="plan-1"))),
                patch.object(routes, "_publish_operation") as publish,
            ):
                response, status_code = routes.execute_retranslation_request.__wrapped__("project-1", "request-1")

        self.assertEqual(status_code, 409)
        self.assertEqual(response.get_json()["code"], "retranslation-await-external")
        publish.assert_not_called()

    def test_retranslation_execute_replays_active_or_completed_link_without_publication(self):
        app = self._app()
        project = self._retranslation_project()
        for operation_state in ("queued", "running", "completed"):
            with self.subTest(operation_state=operation_state):
                request_row = self._retranslation_row(state="started", version=2, operation_id="operation-1")
                operation = self._retranslation_operation(state=operation_state)
                with self._execute_retranslation_context(app, version=1):
                    g.project = project
                    g.access_role = "owner"
                    g.review_actor = SimpleNamespace(role="owner", user_id=7)
                    with (
                        patch.object(routes.scholarly_review, "retranslation_request_record", return_value=request_row),
                        patch.object(routes.scholarly_review, "retranslation_request_projection", return_value={"id": request_row.id, "state": "started", "version": 2}),
                        patch.object(routes, "load_block_source_plan", return_value=SimpleNamespace(record=SimpleNamespace(id="plan-1"))),
                        patch.object(routes, "get_operation", return_value=operation),
                        patch.object(routes, "_publish_operation") as publish,
                        patch.object(routes, "_operation_response", return_value=("replayed", 202)) as operation_response,
                    ):
                        response = routes.execute_retranslation_request.__wrapped__("project-1", "request-1")
                self.assertEqual(response, ("replayed", 202))
                publish.assert_not_called()
                operation_response.assert_called_once()

    def test_retranslation_execute_recovers_failed_link_with_deterministic_key(self):
        app = self._app()
        project = self._retranslation_project()
        request_row = self._retranslation_row(state="started", version=2, operation_id="failed-operation")
        failed = self._retranslation_operation(state="failed", operation_id="failed-operation")
        recovery = self._retranslation_operation(operation_id="recovery-operation")
        source_plan = SimpleNamespace(id="plan-1", plan_hash="b" * 64)
        actor = SimpleNamespace(role="owner", user_id=7)
        captured = {}

        def publish(_project, **kwargs):
            captured.update(kwargs)
            kwargs["initialize_operation"](recovery, True)
            return recovery, None

        dossier_pin = {"revision_id": "dossier-revision-1", "record_hash": "c" * 64, "content_digest": "d" * 64, "conclusion_provenance": "managed_verified", "evidence_verification_state": "verified"}
        with self._execute_retranslation_context(app, version=2):
            g.project = project
            g.access_role = "owner"
            g.review_actor = actor
            with (
                patch.object(routes, "current_user", SimpleNamespace(id=7)),
                patch.object(routes.scholarly_review, "retranslation_request_record", return_value=request_row),
                patch.object(routes.scholarly_review, "retranslation_request_projection", return_value={"id": request_row.id, "state": "started", "version": 2}),
                patch.object(routes, "load_block_source_plan", return_value=SimpleNamespace(record=source_plan)),
                patch.object(routes, "get_operation", return_value=failed),
                patch.object(routes.scholarly_review, "retranslation_dossier_pin", return_value=dossier_pin),
                patch.object(routes, "_validate_block_model_settings"),
                patch.object(routes, "resolve_execution_profile", return_value=SimpleNamespace()),
                patch.object(routes, "derive_window_profile", return_value=SimpleNamespace(to_dict=lambda: {})),
                patch.object(routes.db.session, "get", return_value=SimpleNamespace(id=7)),
                patch.object(routes, "snapshot_protected_targets", return_value={}),
                patch.object(routes, "freeze_block_workflow_config", return_value={}),
                patch.object(routes, "persist_block_workflow_config"),
                patch.object(routes.scholarly_review, "recover_retranslation_request") as recover,
                patch.object(routes, "_block_translation_request_payload", return_value={}),
                patch.object(routes, "_publish_operation", side_effect=publish),
                patch.object(routes, "_operation_response", return_value=("recovered", 202)),
            ):
                response = routes.execute_retranslation_request.__wrapped__("project-1", "request-1")

        self.assertEqual(response, ("recovered", 202))
        self.assertEqual(captured["idempotency_key"], "retranslate-recover-failed-operation")
        recover.assert_called_once_with(
            request_row,
            expected_version=2,
            failed_operation=failed,
            recovery_operation=recovery,
            actor=actor,
        )

    def test_retranslation_execute_does_not_auto_recover_partial_or_cancelled_link(self):
        app = self._app()
        project = self._retranslation_project()
        for operation_state in ("partial", "cancelled"):
            with self.subTest(operation_state=operation_state):
                request_row = self._retranslation_row(state="started", version=2, operation_id="operation-1")
                operation = self._retranslation_operation(state=operation_state)
                with self._execute_retranslation_context(app, version=2):
                    g.project = project
                    g.access_role = "owner"
                    g.review_actor = SimpleNamespace(role="owner", user_id=7)
                    with (
                        patch.object(routes.scholarly_review, "retranslation_request_record", return_value=request_row),
                        patch.object(routes.scholarly_review, "retranslation_request_projection", return_value={"id": request_row.id, "state": "started", "version": 2}),
                        patch.object(routes, "load_block_source_plan", return_value=SimpleNamespace(record=SimpleNamespace(id="plan-1"))),
                        patch.object(routes, "get_operation", return_value=operation),
                        patch.object(routes, "_publish_operation") as publish,
                    ):
                        response, status_code = routes.execute_retranslation_request.__wrapped__("project-1", "request-1")
                self.assertEqual(status_code, 409)
                self.assertEqual(response.get_json()["code"], "retranslation-recovery-conflict")
                publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
