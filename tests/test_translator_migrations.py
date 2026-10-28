from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import analysis_attempts, analysis_execution
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationAnalysisAttempt,
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.models import (
    ApiKey,
    LLMModel,
    LLMProvider,
    RuntimeApprovalRequest,
    User,
    db,
)

persist_analysis_attempt = analysis_attempts.persist_analysis_attempt
rehydrate_analysis_attempt_events = analysis_attempts.rehydrate_analysis_attempt_events
execute_analysis_attempt = analysis_execution.execute_analysis_attempt

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_ROOT = (
    PROJECT_ROOT / "iarf_assistant" / "addons" / "translator" / "migrations"
)


def _migration_config(database_path: Path) -> Config:
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def _migrate_phase5_database(
    database_path: Path,
    instance_path: Path,
    *,
    revision: str = "021_translation_notes",
) -> Flask:
    """Build the real translator migration chain on an isolated SQLite file."""

    config = _migration_config(database_path)
    application = Flask(__name__, instance_path=str(instance_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="migration-phase5-secret",
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    with application.app_context():
        User.__table__.create(db.engine)
        LLMProvider.__table__.create(db.engine)
        ApiKey.__table__.create(db.engine)
        LLMModel.__table__.create(db.engine)
        # RuntimeApprovalRequest carries a core workflow_run FK, while this
        # isolated add-on migration harness intentionally does not load the
        # entire core schema.  Create the minimal referenced parent so FK ON
        # can be enabled for the actual translator chain.
        with db.engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE workflow_run (id VARCHAR(36) PRIMARY KEY)"
            )
        RuntimeApprovalRequest.__table__.create(db.engine)
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        command.upgrade(config, revision)
    return application


def test_block_only_reset_deletes_translator_projects_and_preserves_sources(tmp_path):
    database_path = tmp_path / "translator-block-only-reset.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-block-only-reset",
    )

    with application.app_context():
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id, email, password_hash) "
                    "VALUES (1, 'owner@example.test', 'x')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_project "
                    "(id, user_id, name, source_lang, target_lang, status) "
                    "VALUES ('legacy-project', 1, 'Disposable', 'en', 'pl', 'created')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_segment "
                    "(id, project_id, order_index, block_index, html_tag, "
                    "original_text, status) VALUES "
                    "('legacy-segment', 'legacy-project', 0, 0, 'p', "
                    "'Disposable text', 'pending')"
                )
            )
            # These minimal stand-ins prove that migration 022 stays inside
            # the Translator aggregate. The real tables are owned by other
            # add-ons and therefore are not part of this Alembic chain.
            connection.exec_driver_sql(
                "CREATE TABLE processed_document "
                "(id VARCHAR(36) PRIMARY KEY, title TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE subtitle_project "
                "(id VARCHAR(36) PRIMARY KEY, title TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO processed_document VALUES "
                "('document-source', 'Tenko source')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE mcp_execution ("
                "id VARCHAR(36) PRIMARY KEY, addon_id VARCHAR(32) NOT NULL, "
                "resource_kind VARCHAR(64) NOT NULL, resource_id VARCHAR(255) NOT NULL)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE mcp_work_item ("
                "id VARCHAR(36) PRIMARY KEY, execution_id VARCHAR(36) NOT NULL, "
                "FOREIGN KEY(execution_id) REFERENCES mcp_execution(id) "
                "ON DELETE CASCADE)"
            )
            connection.exec_driver_sql(
                "CREATE TABLE mcp_work_submission ("
                "id VARCHAR(36) PRIMARY KEY, work_item_id VARCHAR(36) NOT NULL, "
                "FOREIGN KEY(work_item_id) REFERENCES mcp_work_item(id) "
                "ON DELETE CASCADE)"
            )
            connection.exec_driver_sql(
                "INSERT INTO mcp_execution VALUES "
                "('translator-execution', 'translator', 'project', "
                "'post-block-project'), "
                "('document-execution', 'doc_processor', 'document', "
                "'document-source')"
            )
            connection.exec_driver_sql(
                "INSERT INTO mcp_work_item VALUES "
                "('translator-work', 'translator-execution'), "
                "('document-work', 'document-execution')"
            )
            connection.exec_driver_sql(
                "INSERT INTO mcp_work_submission VALUES "
                "('translator-submission', 'translator-work'), "
                "('document-submission', 'document-work')"
            )
            connection.exec_driver_sql(
                "INSERT INTO subtitle_project VALUES "
                "('subtitle-source', 'Subtitle source')"
            )

        command.upgrade(config, "head")

        assert db.session.execute(
            text("SELECT COUNT(*) FROM translation_project")
        ).scalar_one() == 0
        assert db.session.execute(
            text("SELECT COUNT(*) FROM translation_segment")
        ).scalar_one() == 0
        assert db.session.execute(
            text("SELECT title FROM processed_document WHERE id = 'document-source'")
        ).scalar_one() == "Tenko source"
        assert db.session.execute(
            text("SELECT id FROM mcp_execution ORDER BY id")
        ).scalars().all() == ["document-execution"]
        assert db.session.execute(
            text("SELECT id FROM mcp_work_item ORDER BY id")
        ).scalars().all() == ["document-work"]
        assert db.session.execute(
            text("SELECT id FROM mcp_work_submission ORDER BY id")
        ).scalars().all() == ["document-submission"]
        assert db.session.execute(
            text("SELECT title FROM subtitle_project WHERE id = 'subtitle-source'")
        ).scalar_one() == "Subtitle source"
        assert "translation_target_block_note" in sqlalchemy_inspect(
            db.engine
        ).get_table_names()
        assert db.session.execute(
            text("SELECT version_num FROM alembic_version_translator")
        ).scalar_one() == "029_epub_editions"

        with pytest.raises(RuntimeError, match="migration 023 is forward-only"):
            command.downgrade(config, "022_block_translator_notes")

        db.session.remove()
        db.engine.dispose()


def test_passive_window_migration_resets_post_block_projects_and_adds_state(tmp_path):
    database_path = tmp_path / "translator-passive-window-reset.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-passive-window-reset",
        revision="022_block_translator_notes",
    )

    with application.app_context():
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id, email, password_hash) "
                    "VALUES (1, 'owner@example.test', 'x')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_project "
                    "(id, user_id, name, source_lang, target_lang, status, "
                    "engine_contract) VALUES "
                    "('post-block-project', 1, 'Disposable', 'en', 'pl', "
                    "'created', 'document-block-v1')"
                )
            )
            # The live post120 failure exposed RESTRICT self-edges in durable
            # attempt lineages. This compact fixture proves the generic purge
            # deletes descendants before their ancestors.
            connection.exec_driver_sql(
                "CREATE TABLE translation_self_fixture ("
                "id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, "
                "parent_id VARCHAR(36), "
                "FOREIGN KEY(project_id) REFERENCES translation_project(id) "
                "ON DELETE CASCADE, "
                "FOREIGN KEY(parent_id) REFERENCES translation_self_fixture(id) "
                "ON DELETE RESTRICT)"
            )
            connection.exec_driver_sql(
                "INSERT INTO translation_self_fixture VALUES "
                "('lineage-root', 'post-block-project', NULL), "
                "('lineage-child', 'post-block-project', 'lineage-root')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE processed_document "
                "(id VARCHAR(36) PRIMARY KEY, title TEXT NOT NULL)"
            )
            connection.exec_driver_sql(
                "INSERT INTO processed_document VALUES "
                "('document-source', 'Tenko source')"
            )

        command.upgrade(config, "head")

        assert db.session.execute(
            text("SELECT COUNT(*) FROM translation_project")
        ).scalar_one() == 0
        assert db.session.execute(
            text("SELECT COUNT(*) FROM translation_self_fixture")
        ).scalar_one() == 0
        assert db.session.execute(
            text("SELECT title FROM processed_document WHERE id = 'document-source'")
        ).scalar_one() == "Tenko source"
        window_attempt_sql = db.session.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'translation_window_attempt'"
            )
        ).scalar_one()
        assert "external_pending" in window_attempt_sql
        assert db.session.execute(
            text("SELECT version_num FROM alembic_version_translator")
        ).scalar_one() == "029_epub_editions"

        db.session.remove()
        db.engine.dispose()


def test_target_edit_idempotency_migration_is_additive_and_cleanly_reversible(
    tmp_path,
):
    database_path = tmp_path / "translator-target-edit-idempotency.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-target-edit-idempotency",
        revision="024_scholarly_review",
    )

    with application.app_context():
        inspector = sqlalchemy_inspect(db.engine)
        assert "translation_target_edit_idempotency" not in inspector.get_table_names()

        command.upgrade(config, "head")
        inspector = sqlalchemy_inspect(db.engine)
        assert "translation_target_edit_idempotency" in inspector.get_table_names()
        assert {
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            "target_revision_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            "request_fingerprint",
            "expected_active_version",
            "active_version",
            "safe_result_json",
            "created_at",
        } == {
            column["name"]
            for column in inspector.get_columns(
                "translation_target_edit_idempotency"
            )
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                "translation_target_edit_idempotency"
            )
        } == {
            "uq_translation_target_edit_idempotency_actor_key",
            "uq_translation_target_edit_idempotency_scope",
        }
        target_revision_fk = next(
            foreign_key
            for foreign_key in inspector.get_foreign_keys(
                "translation_target_edit_idempotency"
            )
            if foreign_key["name"]
            == "fk_translation_target_edit_idempotency_target_revision"
        )
        assert target_revision_fk["options"]["ondelete"] == "CASCADE"

        command.downgrade(config, "024_scholarly_review")
        assert "translation_target_edit_idempotency" not in sqlalchemy_inspect(
            db.engine
        ).get_table_names()
        db.session.remove()
        db.engine.dispose()


def test_target_edit_idempotency_migration_refuses_receipt_loss_on_downgrade(
    tmp_path,
):
    database_path = tmp_path / "translator-target-edit-retained.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-target-edit-retained",
        revision="025_target_edit_idempotency",
    )
    with application.app_context():
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO translation_target_edit_idempotency
                (id, project_id, source_plan_id, block_id, target_revision_id,
                 actor_type, actor_id, idempotency_key, request_fingerprint,
                 expected_active_version, active_version, safe_result_json,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "receipt-1",
                "project-1",
                "plan-1",
                "block-1",
                "revision-1",
                "owner",
                "user:1",
                "retained-edit",
                "f" * 64,
                0,
                1,
                "encrypted-safe-result",
                "2026-09-06T00:00:00",
            ),
        )
        connection.commit()

    with application.app_context(), pytest.raises(
        RuntimeError, match="cannot downgrade after target-edit receipts exist"
    ):
        command.downgrade(config, "024_scholarly_review")

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "025_target_edit_idempotency"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM translation_target_edit_idempotency"
        ).fetchone()[0] == 1


def test_analysis_attempt_migration_clean_downgrade_is_reversible(tmp_path):
    database_path = tmp_path / "translator-analysis-attempt-empty.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(database_path, tmp_path / "instance")
    with application.app_context():
        command.downgrade(config, "011_context_artifacts")
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "021_translation_notes"
        )
        research_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "translation_research_run",
            "translation_research_turn",
            "translation_research_tool_call",
            "translation_research_tool_result_blob",
            "translation_research_tool_call_event",
            "translation_memory_entry",
        } <= research_tables
        analysis_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_analysis_attempt'"
        ).fetchone()[0]
        assert "analysis-attempt-v2" in analysis_sql


def test_evidence_reuse_migration_refuses_downgrade_after_receipt(tmp_path):
    database_path = tmp_path / "translator-evidence-reuse-retained.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path, tmp_path / "instance-evidence-reuse-retained"
    )
    values = {
        "id": "reuse-receipt",
        "contract_version": "translator-operation-evidence-reuse-v1",
        "operation_id": "operation",
        "origin_operation_id": "origin-operation",
        "owner_user_id": 1,
        "project_id": "project",
        "source_plan_id": "source-plan",
        "source_plan_hash": "a" * 64,
        "source_content_hash": "b" * 64,
        "workflow_config_id": "workflow",
        "workflow_config_hash": "c" * 64,
        "origin_workflow_config_id": "origin-workflow",
        "origin_workflow_config_hash": "d" * 64,
        "document_brief_revision_id": "brief",
        "document_brief_revision_hash": "e" * 64,
        "style_guide_revision_id": "style",
        "style_guide_revision_hash": "f" * 64,
        "entity_register_revision_id": "entities",
        "entity_register_revision_hash": "0" * 64,
        "research_run_id": None,
        "research_run_record_hash": None,
        "selection_json": "enc:v2:selection",
        "selection_hash": "1" * 64,
        "selection_receipt": "2" * 64,
        "created_at": "2026-08-30 00:00:00",
    }
    with sqlite3.connect(database_path) as connection:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO translation_operation_evidence_reuse "
            f"({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="migration 019 cannot downgrade"):
            command.downgrade(config, "018_block_workflow_config")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "019_operation_evidence_reuse"
        )


def test_translation_memory_migration_is_empty_evidence_only(tmp_path):
    database_path = tmp_path / "translator-memory-empty.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(database_path, tmp_path / "instance-memory")
    with sqlite3.connect(database_path) as connection:
        memory_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_memory_entry'"
        ).fetchone()[0]
        assert "contextual_precedent" in memory_sql
        assert "deliberate_variant" in memory_sql
        assert "scope_kind = 'project'" in memory_sql
    with application.app_context():
        command.downgrade(config, "014_research_runs")
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        values = {
            "id": "memory-evidence",
            "owner_user_id": 1,
            "project_id": "project",
            "source_plan_id": "plan",
            "block_id": "block",
            "target_revision_id": "revision",
            "target_span_set_id": "span-set",
            "alignment_revision_id": "alignment",
            "source_plan_hash": "a" * 64,
            "target_revision_hash": "b" * 64,
            "target_span_set_hash": "c" * 64,
            "alignment_revision_hash": "d" * 64,
            "active_version": 1,
            "active_pair_receipt": "e" * 64,
            "target_activation_at": "2026-08-26 00:00:00",
            "alignment_activation_at": "2026-08-26 00:00:00",
            "source_language": "en",
            "target_language": "pl",
            "semantic_role": "paragraph",
            "structure_fingerprint": "f" * 64,
            "classification": "contextual_precedent",
            "granularity": "sentence",
            "scope_kind": "project",
            "scope_id": "project",
            "source_lookup_digest": "v1:key:" + "0" * 64,
            "target_lookup_digest": "v1:key:" + "1" * 64,
            "derivation_key": "1" * 64,
            "source_witness_hash": "2" * 64,
            "target_witness_hash": "3" * 64,
            "source_evidence_hash": "4" * 64,
            "target_evidence_hash": "5" * 64,
            "group_ids_hash": "6" * 64,
            "provenance_hash": "7" * 64,
            "record_hash": "8" * 64,
            "source_text": "ciphertext",
            "target_text": "ciphertext",
            "source_evidence_json": "{}",
            "target_evidence_json": "{}",
            "alignment_group_ids_json": "[]",
            "provenance_json": "{}",
            "created_at": "2026-08-26 00:00:00",
        }
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO translation_memory_entry ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="translation-memory evidence"):
            command.downgrade(config, "014_research_runs")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "015_translation_memory"
        )


def test_research_migration_preserves_v1_attempt_events_upgrade_and_downgrade(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "translator-analysis-event-preservation.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-analysis-event-preservation",
        revision="013_integrity_receipts",
    )
    with application.app_context():
        keys = InstallationKeyStore(tmp_path / "event-keys.json")
        keys.initialize()
        application.extensions["carlina_encryption"] = EncryptionService(keys)
        user = User(email="migration-events@example.test", password_hash="x")
        db.session.add(user)
        db.session.flush()
        project = TranslationProject(
            user_id=user.id,
            name="Migration events",
            source_lang="en",
            target_lang="pl",
            engine_contract="document-block-v1",
            settings={
                "source_reference": {
                    "version": 1,
                    "capability": "document.processing",
                    "provider_addon_id": "doc",
                    "provider_capability_id": "process",
                    "resource_type": "document",
                    "resource_id": "migration-doc",
                }
            },
        )
        db.session.add(project)
        db.session.flush()
        html = "<html><body><p>Migration event source.</p></body></html>"
        digest = hashlib.sha256(html.encode()).hexdigest()
        plan = plan_document_blocks(
            AttestedDocumentSnapshot(
                id="migration-doc",
                document_id="migration-doc",
                output_html=html,
                content_hash=digest,
                semantic_content_hash=digest,
                metadata={},
                provenance={},
                captured_at="2026-08-25T00:00:00+00:00",
                semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
            ),
            project_id=project.id,
            source_attestation=source_attestation_for_project(project),
            source_language="en",
        )
        # This fixture intentionally materializes revision-013 state. The
        # explicit active-plan table does not exist until revision 020.
        monkeypatch.setitem(
            persist_source_plan.__globals__,
            "_ensure_initial_active_pointer",
            lambda _project, _record: None,
        )
        monkeypatch.setattr(
            "iarf_assistant.addons.translator.numbering_sync.require_no_numbering_overlay",
            lambda _project_id: None,
        )
        # This historical revision also predates the numbering overlay table.
        # Keep the event-migration fixture on its original neutral projection.
        monkeypatch.setattr(
            "iarf_assistant.addons.translator.numbering_sync.numbering_state",
            lambda _record: {"revision": 0, "entries": []},
        )
        record, _ = persist_source_plan(project, plan)
        operation = TranslationOperation(
            project_id=project.id,
            owner_user_id=user.id,
            kind="document_block_translate",
            idempotency_key="migration-events",
            request_fingerprint="b" * 64,
            state="running",
            queue_job_id="migration-events-worker",
            source_content_hash=plan.snapshot.content_hash,
            source_plan_id=record.id,
            source_plan_hash=plan.plan_hash,
            lease_expires_at=utc_now_db() + timedelta(minutes=5),
        )
        db.session.add(operation)
        db.session.flush()
        attempt = persist_analysis_attempt(
            operation=operation,
            source_plan=record,
            analysis_kind="research",
            contract_version="analysis-attempt-v1",
            packet={
                "messages": [{"role": "user", "content": "Keep exact evidence."}],
                "input": {},
            },
            model={"model_id": "migration/model"},
            profile={"profile": "quality"},
            settings={"temperature": 0},
            budget={"output_tokens": 100},
        )
        db.session.commit()
        execute_analysis_attempt(
            attempt,
            completion_call=lambda _packet: "root response",
        )
        retry = persist_analysis_attempt(
            operation=operation,
            source_plan=record,
            analysis_kind="research",
            contract_version="analysis-attempt-v1",
            packet={
                "messages": [{"role": "user", "content": "Keep exact evidence."}],
                "input": {},
            },
            model={"model_id": "migration/model"},
            profile={"profile": "quality"},
            settings={"temperature": 0},
            budget={"output_tokens": 100},
            retry_of_attempt_id=attempt.id,
        )
        db.session.commit()
        execute_analysis_attempt(
            retry,
            completion_call=lambda _packet: "retry response",
        )
        rebase = persist_analysis_attempt(
            operation=operation,
            source_plan=record,
            analysis_kind="research",
            contract_version="analysis-attempt-v1",
            packet={
                "messages": [{"role": "user", "content": "Changed context."}],
                "input": {},
            },
            model={"model_id": "migration/model"},
            profile={"profile": "quality"},
            settings={"temperature": 0},
            budget={"output_tokens": 100},
            rebase_of_attempt_id=retry.id,
        )
        db.session.commit()
        attempt_ids = (attempt.id, retry.id, rebase.id)
        before = db.session.execute(
            text(
                "SELECT id, attempt_id, sequence, event_type, payload_json, "
                "payload_hash, predecessor_event_hash, event_hash, created_at "
                "FROM translation_analysis_attempt_event "
                "WHERE attempt_id IN (:root, :retry, :rebase) "
                "ORDER BY attempt_id, sequence"
            ),
            {"root": attempt_ids[0], "retry": attempt_ids[1], "rebase": attempt_ids[2]},
        ).all()
        lineage_before = db.session.execute(
            text(
                "SELECT id, retry_of_attempt_id, rebase_of_attempt_id "
                "FROM translation_analysis_attempt "
                "WHERE id IN (:root, :retry, :rebase) ORDER BY id"
            ),
            {"root": attempt_ids[0], "retry": attempt_ids[1], "rebase": attempt_ids[2]},
        ).all()
        assert len(before) == 9
        assert [
            len(rehydrate_analysis_attempt_events(item))
            for item in (attempt, retry, rebase)
        ] == [4, 4, 1]
        db.session.remove()
        db.engine.dispose()
    with application.app_context():
        command.upgrade(config, "021_translation_notes")
    with sqlite3.connect(database_path) as connection:
        after_upgrade = connection.execute(
            "SELECT id, attempt_id, sequence, event_type, payload_json, "
            "payload_hash, predecessor_event_hash, event_hash, created_at "
            "FROM translation_analysis_attempt_event "
            "WHERE attempt_id IN (?, ?, ?) ORDER BY attempt_id, sequence",
            attempt_ids,
        ).fetchall()
        lineage_after_upgrade = connection.execute(
            "SELECT id, retry_of_attempt_id, rebase_of_attempt_id "
            "FROM translation_analysis_attempt WHERE id IN (?, ?, ?) ORDER BY id",
            attempt_ids,
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert after_upgrade == before
    assert lineage_after_upgrade == lineage_before
    with application.app_context():
        restored_keys = InstallationKeyStore(tmp_path / "event-keys.json")
        restored_keys.initialize()
        application.extensions["carlina_encryption"] = EncryptionService(restored_keys)
        # The key store has the same persisted key file and can decrypt the
        # ciphertext after the parent-table rebuild.
        restored = [
            db.session.get(TranslationAnalysisAttempt, item) for item in attempt_ids
        ]
        assert all(item is not None for item in restored)
        assert [len(rehydrate_analysis_attempt_events(item)) for item in restored] == [
            4,
            4,
            1,
        ]
        db.session.remove()
        db.engine.dispose()
    with application.app_context():
        command.downgrade(config, "013_integrity_receipts")
    with sqlite3.connect(database_path) as connection:
        after_downgrade = connection.execute(
            "SELECT id, attempt_id, sequence, event_type, payload_json, "
            "payload_hash, predecessor_event_hash, event_hash, created_at "
            "FROM translation_analysis_attempt_event "
            "WHERE attempt_id IN (?, ?, ?) ORDER BY attempt_id, sequence",
            attempt_ids,
        ).fetchall()
        lineage_after_downgrade = connection.execute(
            "SELECT id, retry_of_attempt_id, rebase_of_attempt_id "
            "FROM translation_analysis_attempt WHERE id IN (?, ?, ?) ORDER BY id",
            attempt_ids,
        ).fetchall()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert after_downgrade == before
    assert lineage_after_downgrade == lineage_before


def test_research_migration_downgrade_refuses_research_evidence(tmp_path):
    database_path = tmp_path / "translator-research-evidence.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path, tmp_path / "instance-research-evidence"
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO translation_research_run (
                id, project_id, owner_user_id, operation_id, source_plan_id,
                source_plan_hash, source_content_hash, contract_version,
                operation_version, queue_job_id, operation_lease_expires_at,
                lease_fence, model_json, settings_json, budget_json, profile_json,
                selected_provider_ids_json, selected_addon_ids_json,
                tool_declarations_json, model_hash, settings_hash, budget_hash,
                providers_hash, addons_hash, tools_hash, profile_hash, run_record_hash,
                state, state_version, state_head_receipt, created_at
            ) VALUES (
                'research-evidence', 'project', 1, 'operation', 'plan',
                :digest, :digest, 'research-run-v1', 1, 'job',
                '2026-08-25 00:00:00', :digest, '{}', '{}', '{}', '{}', '[]', '[]',
                '[]', :digest, :digest, :digest, :digest, :digest, :digest, :digest,
                :digest, 'prepared', 1, :digest, '2026-08-25 00:00:00'
            )
            """,
            {"digest": "a" * 64},
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="research evidence"):
            command.downgrade(config, "013_integrity_receipts")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "014_research_runs"
        )


def test_integrity_receipt_migration_refuses_evidence_and_operation_pins(tmp_path):
    database_path = tmp_path / "translator-integrity-receipts.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "instance-integrity-receipts",
        revision="012_analysis_attempts",
    )

    # Model an orphan row left by a historical SQLite connection with foreign
    # keys disabled.  The migration must inspect every evidence table rather
    # than assuming that a parent source-plan row proves all child evidence.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO translation_target_block_active "
            "(source_plan_id, block_id, revision_id, version, updated_at) "
            "VALUES ('orphan-plan', 'orphan-block', 'orphan-revision', 1, "
            "'2026-08-25 00:00:00')"
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="translation_target_block_active"):
            command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "012_analysis_attempts"
        )
        connection.execute("DELETE FROM translation_target_block_active")
        connection.execute(
            "INSERT INTO translation_operation "
            "(id, project_id, owner_user_id, kind, idempotency_key, "
            "request_fingerprint, state, version, source_content_hash, "
            "source_plan_id, source_plan_hash, created_at, updated_at) VALUES "
            "('pinned-operation', 'orphan-project', 1, "
            "'document_block_translate', 'pinned', ?, 'completed', 1, ?, "
            "'orphan-plan', ?, '2026-08-25 00:00:00', "
            "'2026-08-25 00:00:00')",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="source-plan pin"):
            command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "012_analysis_attempts"
        )
        connection.execute(
            "DELETE FROM translation_operation WHERE id = 'pinned-operation'"
        )
        connection.commit()

    with application.app_context():
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "021_translation_notes"
        )
        connection.execute(
            "INSERT INTO translation_target_block_active "
            "(source_plan_id, block_id, revision_id, version, pair_receipt, "
            "updated_at) VALUES ('orphan-plan', 'orphan-block', "
            "'orphan-revision', 1, ?, '2026-08-25 00:00:00')",
            ("d" * 64,),
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="translation_target_block_active"):
            command.downgrade(config, "012_analysis_attempts")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "013_integrity_receipts"
        )
        connection.execute("DELETE FROM translation_target_block_active")
        connection.commit()
    with application.app_context():
        command.downgrade(config, "012_analysis_attempts")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "012_analysis_attempts"
        )


def test_phase5_operation_plan_pin_is_enforced_by_upgraded_sqlite(tmp_path):
    database_path = tmp_path / "translator-operation-plan-pin.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(database_path, tmp_path / "instance")
    with application.app_context():
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id, email, password_hash) VALUES (1, 'owner@example.test', 'x')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_project (id, user_id, name, source_lang, target_lang, status) "
                    "VALUES ('project', 1, 'P', 'en', 'pl', 'created')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_source_plan "
                    "(id, project_id, contract_version, source_document_id, source_language, "
                    "language_profile, namespace, snapshot_contract_version, "
                    "semantic_contract_version, semantic_contract_mode, semantic_content_hash, "
                    "source_content_hash, snapshot_captured_at, planner_version, "
                    "planner_settings_hash, plan_hash, source_attestation, planner_settings, "
                    "coverage_witness, block_count, anchor_count, created_at) VALUES "
                    "('plan', 'project', 'document-block-v1', 'document', 'en', 'default', "
                    "'namespace', 'snapshot-v1', 'semantic-html-v1', 'strict', :digest, :digest, "
                    "CURRENT_TIMESTAMP, 'planner-v1', :digest, :digest, '{}', '{}', '{}', 0, 0, "
                    "CURRENT_TIMESTAMP)"
                ),
                {"digest": "c" * 64},
            )

            def operation_sql(
                operation_id: str,
                plan_id: str | None,
                plan_hash: str | None,
                content_hash: str | None = None,
            ) -> None:
                connection.execute(
                    text(
                        "INSERT INTO translation_operation "
                        "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, state, "
                        "source_content_hash, source_plan_id, source_plan_hash, created_at, updated_at) "
                        "VALUES (:id, 'project', 1, 'document_block_translate', :id, :digest, 'completed', "
                        ":content, :plan, :hash, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": operation_id,
                        "content": content_hash,
                        "plan": plan_id,
                        "hash": plan_hash,
                        "digest": "a" * 64,
                    },
                )

            # Historical operations may retain their pre-plan content digest,
            # or may have no source digest at all; neither is a plan pin.
            operation_sql("legacy-unpinned", None, None, "b" * 64)
            connection.execute(
                text(
                    "INSERT INTO translation_operation "
                    "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, state, "
                    "source_content_hash, source_plan_id, source_plan_hash, created_at, updated_at) "
                    "VALUES ('complete', 'project', 1, 'document_block_translate', 'complete', :digest, "
                    "'completed', :digest, 'plan', :digest, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"digest": "c" * 64},
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO translation_operation "
                        "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, state, "
                        "source_content_hash, source_plan_id, source_plan_hash, created_at, updated_at) "
                        "VALUES ('plan-id-only', 'project', 1, 'document_block_translate', 'plan-id-only', :digest, "
                        "'queued', NULL, 'plan', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"digest": "c" * 64},
                )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO translation_operation "
                        "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, state, "
                        "source_content_hash, source_plan_id, source_plan_hash, created_at, updated_at) "
                        "VALUES ('plan-hash-only', 'project', 1, 'document_block_translate', 'plan-hash-only', :digest, "
                        "'queued', NULL, NULL, :digest, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"digest": "c" * 64},
                )
            with pytest.raises(IntegrityError):
                operation_sql("unpaired", None, "b" * 64)
            with pytest.raises(IntegrityError):
                operation_sql("foreign", "missing-plan", "b" * 64, "b" * 64)
            with pytest.raises(IntegrityError):
                operation_sql("short", "missing-plan", "short", "b" * 64)
        inspector = sqlalchemy_inspect(db.engine)
        check_names = {
            item["name"]
            for item in inspector.get_check_constraints("translation_operation")
        }
        foreign_keys = {
            item["name"] for item in inspector.get_foreign_keys("translation_operation")
        }
        assert "ck_translation_operation_source_plan_pin" in check_names
        assert "fk_translation_operation_source_plan_project" in foreign_keys
        assert db.session.execute(
            text(
                "SELECT source_content_hash, source_plan_id, source_plan_hash "
                "FROM translation_operation WHERE id = 'legacy-unpinned'"
            )
        ).one() == ("b" * 64, None, None)
        with pytest.raises(RuntimeError, match="operation source pin evidence"):
            command.downgrade(config, "010_translation_target_alignment")
        db.session.remove()
        db.engine.dispose()


def test_translator_index_migration_preserves_projects_and_segments(tmp_path):
    database_path = tmp_path / "translator-migration.db"
    config = _migration_config(database_path)
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="migration-test-secret",
    )
    db.init_app(application)

    with application.app_context():
        User.__table__.create(db.engine)
        LLMProvider.__table__.create(db.engine)
        ApiKey.__table__.create(db.engine)
        LLMModel.__table__.create(db.engine)
        RuntimeApprovalRequest.__table__.create(db.engine)
        command.upgrade(config, "004_glossary_needs_review")
        with db.engine.begin() as connection:
            connection.execute(
                User.__table__.insert(),
                {"id": 1, "email": "owner@example.test", "password_hash": "x"},
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_project
                        (id, user_id, name, source_lang, target_lang, status)
                    VALUES ('project-1', 1, 'Book', 'en', 'pl', 'created')
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_segment
                        (id, project_id, order_index, block_index, html_tag,
                         original_text, status)
                    VALUES ('segment-1', 'project-1', 0, 0, 'p', 'Hello', 'pending')
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_segment
                        (id, project_id, order_index, block_index, html_tag,
                         original_text, status, merged_into_id)
                    VALUES ('segment-2', 'project-1', 1, 0, 'p', 'World',
                            'pending', 'segment-1')
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_revision
                        (id, segment_id, translated_text, author_type,
                         user_id)
                    VALUES ('revision-1', 'segment-1', 'Czesc', 'human', 1)
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_glossary
                        (id, project_id, source_term, target_term,
                         is_user_defined, needs_review)
                    VALUES (1, 'project-1', 'Hello', 'Czesc', 1, 0)
                    """
                )
            )

        # Keep this regression focused on the 006 operation migration.  The
        # additive block-engine schema has its own migration test below.
        command.upgrade(config, "006_translation_operations")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version_translator"
        ).fetchone()[0]
        project_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('translation_project')")
        }
        segment_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('translation_segment')")
        }
        glossary_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('translation_glossary')")
        }
        revision_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('translation_revision')")
        }
        preserved = connection.execute(
            "SELECT project_id, original_text FROM translation_segment WHERE id = ?",
            ("segment-1",),
        ).fetchone()
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "translation_project",
                "translation_segment",
                "translation_revision",
                "translation_glossary",
            )
        }
        segment_versions = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT id, version FROM translation_segment ORDER BY id"
            )
        }

    assert revision == "006_translation_operations"
    assert "ix_translation_project_user_updated" in project_indexes
    assert "uq_translation_project_user_creation_key" in project_indexes
    assert "ix_translation_segment_project_order" in segment_indexes
    assert "ix_translation_segment_project_status_order" in segment_indexes
    assert "ix_translation_glossary_project_source" in glossary_indexes
    assert "ix_translation_revision_segment_created" in revision_indexes
    with sqlite3.connect(database_path) as connection:
        project_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_project')")
        }
        segment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_segment')")
        }
        operation_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_operation')")
        }
        operation_indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list('translation_operation')")
        }
        active_index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'uq_translation_operation_active_project'"
        ).fetchone()[0]
    assert {
        "source_snapshot",
        "source_content_hash",
        "source_captured_at",
        "creation_idempotency_key",
        "creation_request_fingerprint",
    } <= project_columns
    assert "version" in segment_columns
    assert {
        "id",
        "project_id",
        "owner_user_id",
        "idempotency_key",
        "request_fingerprint",
        "state",
        "version",
        "lease_expires_at",
        "checkpoint_json",
        "result_json",
    } <= operation_columns
    assert "ix_translation_operation_project_state_updated" in operation_indexes
    assert "ix_translation_operation_owner_kind_created" in operation_indexes
    assert "UNIQUE INDEX uq_translation_operation_active_project" in active_index_sql
    assert "state IN ('queued', 'running', 'cancel_requested')" in active_index_sql
    assert preserved == ("project-1", "Hello")
    assert counts == {
        "translation_project": 1,
        "translation_segment": 2,
        "translation_revision": 1,
        "translation_glossary": 1,
    }
    assert segment_versions == {"segment-1": 1, "segment-2": 1}

    with sqlite3.connect(database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE translation_segment SET version = 0 WHERE id = 'segment-1'"
            )
        connection.rollback()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE translation_project SET source_snapshot = ?, "
            "source_content_hash = ? WHERE id = 'project-1'",
            ("enc:v2:durable-snapshot", "a" * 64),
        )
        connection.commit()

    with application.app_context():
        with pytest.raises(RuntimeError, match="forward-only"):
            command.downgrade(config, "005_query_indexes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "006_translation_operations"
        )
        assert (
            connection.execute(
                "SELECT source_snapshot FROM translation_project WHERE id = 'project-1'"
            ).fetchone()[0]
            == "enc:v2:durable-snapshot"
        )
        connection.execute(
            "UPDATE translation_project SET source_snapshot = NULL, "
            "source_content_hash = NULL WHERE id = 'project-1'"
        )
        connection.commit()

    with application.app_context():
        command.downgrade(config, "005_query_indexes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        downgraded_revision = connection.execute(
            "SELECT version_num FROM alembic_version_translator"
        ).fetchone()[0]
        downgraded_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "translation_project",
                "translation_segment",
                "translation_revision",
                "translation_glossary",
            )
        }
        downgraded_project_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_project')")
        }
        downgraded_segment_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_segment')")
        }
    assert downgraded_revision == "005_query_indexes"
    assert downgraded_counts == counts
    assert (
        not {
            "source_snapshot",
            "source_content_hash",
            "source_captured_at",
            "creation_idempotency_key",
            "creation_request_fingerprint",
        }
        & downgraded_project_columns
    )
    assert "version" not in downgraded_segment_columns


def test_translator_source_plan_migration_is_additive_and_forward_only(tmp_path):
    database_path = tmp_path / "translator-source-plan.db"
    config = _migration_config(database_path)
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="migration-test-secret",
    )
    db.init_app(application)

    with application.app_context():
        User.__table__.create(db.engine)
        LLMProvider.__table__.create(db.engine)
        ApiKey.__table__.create(db.engine)
        LLMModel.__table__.create(db.engine)
        RuntimeApprovalRequest.__table__.create(db.engine)
        command.upgrade(config, "006_translation_operations")
        with db.engine.begin() as connection:
            connection.execute(
                User.__table__.insert(),
                {"id": 1, "email": "owner@example.test", "password_hash": "x"},
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_project
                        (id, user_id, name, source_lang, target_lang, status)
                    VALUES ('project-1', 1, 'Book', 'en', 'pl', 'created')
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_segment
                        (id, project_id, order_index, block_index, html_tag,
                         original_text, status)
                    VALUES ('segment-1', 'project-1', 0, 0, 'p', 'Hello', 'pending')
                    """
                )
            )
            connection.execute(
                db.text(
                    """
                    INSERT INTO translation_operation
                        (id, project_id, owner_user_id, kind, idempotency_key,
                         request_fingerprint, state, source_content_hash,
                         created_at, updated_at)
                    VALUES ('legacy-operation', 'project-1', 1,
                            'translate', 'legacy-operation', :fingerprint,
                            'completed', :source_hash,
                            '2026-08-25 00:00:00', '2026-08-25 00:00:00')
                    """
                ),
                {"fingerprint": "f" * 64, "source_hash": "e" * 64},
            )

        legacy_project = db.session.execute(
            db.text(
                "SELECT id, name, source_lang, target_lang, status "
                "FROM translation_project WHERE id = 'project-1'"
            )
        ).one()
        legacy_segment = db.session.execute(
            db.text(
                "SELECT id, project_id, order_index, original_text "
                "FROM translation_segment WHERE id = 'segment-1'"
            )
        ).one()
        db.session.remove()
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version_translator"
        ).fetchone()[0]
        project_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info('translation_project')")
        }
        table_columns = {
            table: {
                row[1]: row
                for row in connection.execute(f"PRAGMA table_info('{table}')")
            }
            for table in (
                "translation_source_plan",
                "translation_block",
                "translation_source_anchor",
                "translation_target_block_revision",
                "translation_target_block_active",
                "translation_window_attempt",
                "translation_window_attempt_event",
                "translation_window_attempt_revision",
                "translation_target_span_set",
                "translation_target_span",
                "translation_alignment_revision",
                "translation_alignment_group",
                "translation_target_alignment_active",
                "translation_analysis_attempt",
                "translation_analysis_attempt_event",
                "translation_note",
            )
        }
        indexes = {
            table: {
                row[1] for row in connection.execute(f"PRAGMA index_list('{table}')")
            }
            for table in table_columns
        }
        project = connection.execute(
            "SELECT id, name, source_lang, target_lang, status, engine_contract "
            "FROM translation_project WHERE id = 'project-1'"
        ).fetchone()
        segment = connection.execute(
            "SELECT id, project_id, order_index, original_text "
            "FROM translation_segment WHERE id = 'segment-1'"
        ).fetchone()
        legacy_operation = connection.execute(
            "SELECT source_content_hash, source_plan_id, source_plan_hash "
            "FROM translation_operation WHERE id = 'legacy-operation'"
        ).fetchone()
        project_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_source_plan'"
        ).fetchone()[0]
        block_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_block'"
        ).fetchone()[0]
        anchor_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_source_anchor'"
        ).fetchone()[0]
        note_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_note'"
        ).fetchone()[0]
        target_revision_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_target_block_revision'"
        ).fetchone()[0]
        target_active_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_target_block_active'"
        ).fetchone()[0]
        window_attempt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_window_attempt'"
        ).fetchone()[0]
        window_event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_window_attempt_event'"
        ).fetchone()[0]
        window_revision_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_window_attempt_revision'"
        ).fetchone()[0]
        alignment_span_set_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_target_span_set'"
        ).fetchone()[0]
        alignment_revision_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_alignment_revision'"
        ).fetchone()[0]
        alignment_span_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_target_span'"
        ).fetchone()[0]
        alignment_group_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_alignment_group'"
        ).fetchone()[0]
        alignment_active_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_target_alignment_active'"
        ).fetchone()[0]
        analysis_attempt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_analysis_attempt'"
        ).fetchone()[0]
        analysis_event_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_analysis_attempt_event'"
        ).fetchone()[0]
        research_tool_call_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_research_tool_call'"
        ).fetchone()[0]
        research_result_blob_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'translation_research_tool_result_blob'"
        ).fetchone()[0]

        assert revision == "021_translation_notes"
    assert "ck_translation_analysis_attempt_dispatch_state" in analysis_attempt_sql
    assert "dispatch_receipt_hash" in analysis_attempt_sql
    assert "fk_translation_analysis_attempt_operation_scope" in analysis_attempt_sql
    assert "uq_translation_analysis_attempt_event_sequence" in analysis_event_sql
    assert "provider_outcome_uncertain" in analysis_event_sql
    assert "tool_call_finished" not in analysis_event_sql
    assert (
        "ck_translation_research_tool_call_operation_version" in research_tool_call_sql
    )
    assert "ck_translation_research_tool_call_argument_state" in research_tool_call_sql
    assert "provider_call_id_json" in research_tool_call_sql
    assert "turn_call_index" in research_tool_call_sql
    assert (
        "ck_translation_research_tool_result_blob_payload_hash"
        in research_result_blob_sql
    )
    assert "fk_translation_research_tool_result_blob_call" in research_result_blob_sql
    assert project == (*legacy_project, None)
    assert segment == legacy_segment
    assert legacy_operation == ("e" * 64, None, None)
    assert project_columns["engine_contract"][2].upper() == "VARCHAR(64)"
    assert project_columns["engine_contract"][4] is None
    assert {
        "id",
        "project_id",
        "contract_version",
        "source_document_id",
        "source_filename",
        "source_language",
        "language_profile",
        "namespace",
        "snapshot_contract_version",
        "semantic_contract_version",
        "semantic_contract_mode",
        "semantic_content_hash",
        "source_content_hash",
        "snapshot_captured_at",
        "source_updated_at",
        "planner_version",
        "planner_settings_hash",
        "plan_hash",
        "source_metadata",
        "source_attestation",
        "planner_settings",
        "source_provenance",
        "coverage_witness",
        "block_count",
        "anchor_count",
        "created_at",
    } == set(table_columns["translation_source_plan"])
    assert {
        "id",
        "source_plan_id",
        "order_index",
        "role",
        "locator",
        "classification_summary",
        "source_fragment",
        "source_projection",
        "source_slots",
        "protected_inline",
        "fragment_hash",
        "projection_hash",
        "created_at",
    } == set(table_columns["translation_block"])
    assert {
        "id",
        "source_plan_id",
        "block_id",
        "order_index",
        "block_order_index",
        "start_offset",
        "end_offset",
        "splitter_profile",
        "splitter_version",
        "source_text",
        "source_hash",
        "created_at",
    } == set(table_columns["translation_source_anchor"])
    assert {
        "id",
        "project_id",
        "segment_id",
        "order_index",
        "anchor_text",
        "body",
        "author_type",
        "state",
        "segment_version",
        "version",
        "created_at",
        "updated_at",
    } == set(table_columns["translation_note"])
    assert {
        "ix_translation_note_segment_state_version_order",
        "ix_translation_note_project_segment",
    } <= indexes["translation_note"]
    for column in ("anchor_text", "body"):
        assert table_columns["translation_note"][column][2].upper() == "TEXT"
    for constraint_name in (
        "uq_translation_note_segment_version_order",
        "ck_translation_note_order_index",
        "ck_translation_note_segment_version",
        "ck_translation_note_version",
        "ck_translation_note_author_type",
        "ck_translation_note_state",
        "fk_translation_note_project",
        "fk_translation_note_segment",
    ):
        assert constraint_name in note_sql
    assert {
        "id",
        "source_plan_id",
        "block_id",
        "source_plan_hash",
        "structural_fingerprint",
        "contract_version",
        "validator_version",
        "parent_revision_id",
        "parent_revision_hash",
        "author_type",
        "author_user_id",
        "validation_state",
        "target_language",
        "normalized_candidate",
        "rendered_fragment",
        "validation_evidence",
        "source_derived_target_text",
        "candidate_hash",
        "normalized_hash",
        "fragment_hash",
        "evidence_hash",
        "revision_record_hash",
        "created_at",
    } == set(table_columns["translation_target_block_revision"])
    assert {
        "source_plan_id",
        "block_id",
        "revision_id",
        "version",
        "pair_receipt",
        "updated_at",
    } == set(table_columns["translation_target_block_active"])
    assert {
        "id",
        "project_id",
        "owner_user_id",
        "operation_id",
        "source_plan_id",
        "source_plan_hash",
        "source_content_hash",
        "operation_version",
        "queue_job_id",
        "operation_lease_expires_at",
        "lease_fence",
        "window_order",
        "packet_json",
        "settings_json",
        "block_ids_json",
        "block_fingerprints_json",
        "predecessor_json",
        "model_profile_json",
        "admission_json",
        "context_manifest_json",
        "packet_hash",
        "prompt_hash",
        "settings_hash",
        "context_hash",
        "context_manifest_hash",
        "model_profile_hash",
        "predecessor_hash",
        "retry_of_attempt_id",
        "retry_of_attempt_hash",
        "rebase_of_attempt_id",
        "rebase_of_attempt_hash",
        "attempt_record_hash",
        "state_head_receipt",
        "dispatch_state",
        "dispatch_version",
        "dispatch_claimed_at",
        "dispatch_finished_at",
        "created_at",
    } == set(table_columns["translation_window_attempt"])
    assert {
        "id",
        "attempt_id",
        "sequence",
        "event_type",
        "payload_json",
        "payload_hash",
        "predecessor_event_hash",
        "event_hash",
        "created_at",
    } == set(table_columns["translation_window_attempt_event"])
    assert {
        "id",
        "attempt_id",
        "source_plan_id",
        "block_id",
        "revision_id",
        "candidate_hash",
        "revision_record_hash",
        "created_at",
    } == set(table_columns["translation_window_attempt_revision"])
    assert {
        "id",
        "source_plan_id",
        "block_id",
        "revision_id",
        "target_revision_hash",
        "projection_scope",
        "profile_id",
        "profile_version",
        "projection",
        "projection_hash",
        "span_count",
        "record_hash",
        "created_at",
    } == set(table_columns["translation_target_span_set"])
    assert {
        "id",
        "span_set_id",
        "source_plan_id",
        "block_id",
        "revision_id",
        "order_index",
        "start_offset",
        "end_offset",
        "text_hash",
        "span_hash",
    } == set(table_columns["translation_target_span"])
    assert {
        "id",
        "source_plan_id",
        "block_id",
        "revision_id",
        "span_set_id",
        "target_revision_hash",
        "span_set_hash",
        "method",
        "confidence_profile",
        "review_state",
        "group_count",
        "payload",
        "groups_hash",
        "record_hash",
        "created_at",
    } == set(table_columns["translation_alignment_revision"])
    assert {
        "id",
        "alignment_revision_id",
        "source_plan_id",
        "block_id",
        "revision_id",
        "span_set_id",
        "order_index",
        "source_anchor_ids",
        "target_span_ids",
        "source_witness_hash",
        "target_witness_hash",
        "method",
        "confidence",
        "review_state",
        "evidence",
        "evidence_hash",
        "group_hash",
    } == set(table_columns["translation_alignment_group"])
    assert {
        "source_plan_id",
        "block_id",
        "revision_id",
        "alignment_revision_id",
        "version",
        "updated_at",
    } == set(table_columns["translation_target_alignment_active"])
    assert {
        "ix_translation_source_plan_project_created",
    } <= indexes["translation_source_plan"]
    assert {"ix_translation_block_plan_order"} <= indexes["translation_block"]
    assert {
        "ix_translation_source_anchor_plan_order",
        "ix_translation_source_anchor_block_order",
    } <= indexes["translation_source_anchor"]
    for table, encrypted_columns in {
        "translation_source_plan": {
            "source_filename",
            "source_metadata",
            "source_attestation",
            "planner_settings",
        },
        "translation_block": {
            "source_fragment",
            "source_projection",
            "source_slots",
            "protected_inline",
        },
        "translation_source_anchor": {"source_text"},
        "translation_target_block_revision": {
            "normalized_candidate",
            "rendered_fragment",
            "validation_evidence",
            "source_derived_target_text",
        },
        "translation_window_attempt": {
            "packet_json",
            "settings_json",
            "block_ids_json",
            "block_fingerprints_json",
            "predecessor_json",
            "model_profile_json",
            "admission_json",
            "context_manifest_json",
        },
        "translation_window_attempt_event": {"payload_json"},
        "translation_target_span_set": {"projection"},
        "translation_alignment_revision": {"payload"},
        "translation_alignment_group": {
            "source_anchor_ids",
            "target_span_ids",
            "evidence",
        },
    }.items():
        for column in encrypted_columns:
            assert table_columns[table][column][2].upper() == "TEXT"
    assert table_columns["translation_source_plan"]["source_provenance"][2].upper() in {
        "JSON",
        "TEXT",
    }
    assert table_columns["translation_source_plan"]["coverage_witness"][2].upper() in {
        "JSON",
        "TEXT",
    }
    for constraint_name in (
        "uq_translation_source_plan_project_plan_hash",
        "ck_translation_source_plan_plan_hash",
        "ck_translation_source_plan_planner_settings_hash",
        "ck_translation_source_plan_block_count",
        "uq_translation_block_plan_order",
        "uq_translation_block_id_source_plan",
        "ck_translation_block_order_index",
        "ck_translation_block_projection_hash",
        "fk_translation_source_anchor_block_plan",
        "uq_translation_source_anchor_plan_order",
        "uq_translation_source_anchor_block_order",
        "ck_translation_source_anchor_order_index",
        "ck_translation_source_anchor_block_order_index",
        "ck_translation_source_anchor_end_offset",
    ):
        assert constraint_name in project_sql + block_sql + anchor_sql
    for constraint_name in (
        "ck_translation_target_revision_record_hash",
        "ck_translation_target_revision_parent_hash",
        "ck_translation_target_revision_parent_witness",
        "fk_translation_target_revision_block_plan",
        "fk_translation_target_active_revision",
        "ck_translation_target_active_version",
        "ck_translation_target_active_pair_receipt",
        "ck_translation_window_attempt_retry_witness",
        "ck_translation_window_attempt_single_lineage",
        "ck_translation_window_attempt_state_head_receipt",
        "ck_translation_window_attempt_source_hash",
        "ck_translation_window_attempt_operation_version",
        "ck_translation_window_attempt_queue_job",
        "ck_translation_window_attempt_lease_fence",
        "ck_translation_window_attempt_dispatch_state",
        "fk_translation_window_attempt_event_attempt",
        "uq_translation_window_attempt_id_plan",
        "fk_translation_window_attempt_revision_attempt_plan",
        "fk_translation_window_attempt_revision_revision_plan_block",
        "fk_translation_target_span_set_revision_plan_block",
        "fk_translation_target_span_set_identity",
        "fk_translation_alignment_revision_span_set_identity",
        "fk_translation_alignment_group_revision_identity",
        "fk_translation_target_alignment_active_alignment",
    ):
        assert constraint_name in (
            target_revision_sql
            + target_active_sql
            + window_attempt_sql
            + window_event_sql
            + window_revision_sql
            + alignment_span_set_sql
            + alignment_span_sql
            + alignment_revision_sql
            + alignment_group_sql
            + alignment_active_sql
        )

    with application.app_context():
        # No plan evidence and a NULL engine contract are safe to downgrade;
        # this is the compatibility path for all existing projects.
        command.downgrade(config, "006_translation_operations")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "006_translation_operations"
        )
        assert "engine_contract" not in {
            row[1]
            for row in connection.execute("PRAGMA table_info('translation_project')")
        }
        remaining_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert (
            not {
                "translation_source_plan",
                "translation_block",
                "translation_source_anchor",
            }
            & remaining_tables
        )

    with application.app_context():
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE translation_project SET engine_contract = ? WHERE id = 'project-1'",
            ("document-block-v1",),
        )
        connection.execute(
            """
            INSERT INTO translation_source_plan
                (id, project_id, contract_version, source_document_id,
                 source_language, language_profile, namespace,
                 snapshot_contract_version,
                 semantic_contract_version, semantic_contract_mode,
                 semantic_content_hash, source_content_hash,
                 snapshot_captured_at, planner_version, planner_settings_hash,
                 plan_hash, source_metadata, source_attestation,
                 planner_settings, source_provenance, coverage_witness,
                 block_count, anchor_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plan-1",
                "project-1",
                "translation-source-plan-v1",
                "document-1",
                "en",
                "en-default-v1",
                "00000000-0000-0000-0000-000000000001",
                "document-snapshot-v1",
                "semantic-html-v1",
                "completed",
                "a" * 64,
                "b" * 64,
                "2026-08-25T00:00:00",
                "planner-v1",
                "c" * 64,
                "d" * 64,
                "{}",
                '{"version":"document-snapshot-v1","capability":"document.processing","provider_id":"doc-provider","resource_type":"document","resource_id":"document-1","owner_id":"1"}',
                '{"source_language":"en","language_profile":"en-default-v1"}',
                "{}",
                "{}",
                0,
                0,
                "2026-08-25T00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO translation_block
                (id, source_plan_id, order_index, role, locator,
                 classification_summary, source_fragment, source_projection,
                 source_slots, protected_inline, fragment_hash,
                 projection_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "block-1",
                "plan-1",
                1,
                "paragraph",
                "{}",
                "{}",
                "Hello",
                "Hello",
                "{}",
                "{}",
                "e" * 64,
                "f" * 64,
                "2026-08-25T00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO translation_source_anchor
                (id, source_plan_id, block_id, order_index,
                 block_order_index, start_offset, end_offset,
                 splitter_profile, splitter_version, source_text,
                 source_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "anchor-1",
                "plan-1",
                "block-1",
                1,
                1,
                0,
                5,
                "default",
                "v1",
                "Hello",
                "a" * 64,
                "2026-08-25T00:00:00",
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE translation_block SET order_index = 0 WHERE id = 'block-1'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE translation_source_anchor SET block_order_index = 0 "
                "WHERE id = 'anchor-1'"
            )
        connection.rollback()

        # Migration 009 must enforce the same cross-plan identity as the ORM,
        # not merely rely on later rehydration to notice a corrupt audit link.
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO translation_operation
                (id, project_id, owner_user_id, kind, idempotency_key,
                 request_fingerprint, state, version, created_at, updated_at)
            VALUES ('operation-1', 'project-1', 1, 'document_block_translate',
                    'migration-fk', ?, 'completed', 1, ?, ?)
            """,
            ("1" * 64, "2026-08-25T00:00:00", "2026-08-25T00:00:00"),
        )
        connection.execute(
            """
            INSERT INTO translation_source_plan
                (id, project_id, contract_version, source_document_id,
                 source_language, language_profile, namespace,
                 snapshot_contract_version, semantic_contract_version,
                 semantic_contract_mode, semantic_content_hash,
                 source_content_hash, snapshot_captured_at, planner_version,
                 planner_settings_hash, plan_hash, source_metadata,
                 source_attestation, planner_settings, source_provenance,
                 coverage_witness, block_count, anchor_count, created_at)
            SELECT 'plan-2', project_id, contract_version, 'document-2',
                   source_language, language_profile,
                   '00000000-0000-0000-0000-000000000002',
                   snapshot_contract_version, semantic_contract_version,
                   semantic_contract_mode, semantic_content_hash,
                   source_content_hash, snapshot_captured_at, planner_version,
                   planner_settings_hash, ?, source_metadata,
                   source_attestation, planner_settings, source_provenance,
                   coverage_witness, 1, 0, created_at
            FROM translation_source_plan WHERE id = 'plan-1'
            """,
            ("2" * 64,),
        )
        connection.execute(
            """
            INSERT INTO translation_block
                (id, source_plan_id, order_index, role, locator,
                 classification_summary, source_fragment, source_projection,
                 source_slots, protected_inline, fragment_hash,
                 projection_hash, created_at)
            VALUES ('block-2', 'plan-2', 1, 'paragraph', '{}', '{}',
                    'Other', 'Other', '{}', '{}', ?, ?, ?)
            """,
            ("3" * 64, "4" * 64, "2026-08-25T00:00:00"),
        )
        connection.execute(
            """
            INSERT INTO translation_target_block_revision
                (id, source_plan_id, block_id, source_plan_hash,
                 structural_fingerprint, contract_version, validator_version,
                 author_type, validation_state, target_language,
                 normalized_candidate, rendered_fragment,
                 validation_evidence, source_derived_target_text,
                 candidate_hash, normalized_hash, fragment_hash,
                 evidence_hash, revision_record_hash, created_at)
            VALUES ('revision-2', 'plan-2', 'block-2', ?, ?,
                    'target-block-result-v1', 'target-block-validator-v1',
                    'system', 'valid', 'pl', '{}', 'Inny', '{}', 'Inny',
                    ?, ?, ?, ?, ?, ?)
            """,
            tuple(value * 64 for value in "2345678") + ("2026-08-25T00:00:00",),
        )
        connection.execute(
            """
            INSERT INTO translation_window_attempt
                (id, project_id, owner_user_id, operation_id, source_plan_id,
                 source_plan_hash, source_content_hash, operation_version, queue_job_id,
                 operation_lease_expires_at, lease_fence, window_order, packet_json,
                 settings_json, block_ids_json,
                 block_fingerprints_json, predecessor_json, model_profile_json,
                 admission_json, context_manifest_json, packet_hash,
                 prompt_hash, settings_hash, context_hash,
                 context_manifest_hash, model_profile_hash, predecessor_hash,
                 attempt_record_hash, state_head_receipt, created_at)
            VALUES ('attempt-1', 'project-1', 1, 'operation-1', 'plan-1',
                    ?, ?, 1, 'legacy-window-attempt-1', ?, ?, 1,
                    '{}', '{}', '[]', '[]', '{}', '{}', '{}', '[]',
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(value * 64 for value in "dabcdef012345") + ("2026-08-25T00:00:00",),
        )
        connection.commit()
        # Migration 010 makes the alignment chain fail at the database boundary
        # too: a target revision may not be relabelled into another plan/block.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO translation_target_span_set
                    (id, source_plan_id, block_id, revision_id,
                     target_revision_hash, profile_id, profile_version,
                     projection, projection_hash, span_count, record_hash, created_at)
                VALUES ('span-set-cross-plan', 'plan-1', 'block-1', 'revision-2',
                        ?, 'target-english', 'target-splitter-v1',
                        'enc:v2:projection', ?, 0, ?, ?)
                """,
                ("a" * 64, "b" * 64, "c" * 64, "2026-08-25T00:00:00"),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO translation_window_attempt_revision
                    (id, attempt_id, source_plan_id, block_id, revision_id,
                     candidate_hash, revision_record_hash, created_at)
                VALUES ('link-cross-plan', 'attempt-1', 'plan-2', 'block-2',
                        'revision-2', ?, ?, ?)
                """,
                ("9" * 64, "8" * 64, "2026-08-25T00:00:00"),
            )
        connection.rollback()
        connection.execute(
            "DELETE FROM translation_window_attempt WHERE id = 'attempt-1'"
        )
        connection.execute(
            "DELETE FROM translation_target_block_revision WHERE id = 'revision-2'"
        )
        connection.execute("DELETE FROM translation_block WHERE id = 'block-2'")
        connection.execute("DELETE FROM translation_source_plan WHERE id = 'plan-2'")
        connection.execute("DELETE FROM translation_operation WHERE id = 'operation-1'")
        connection.commit()

    with application.app_context():
        with pytest.raises(RuntimeError, match="migration 013 cannot downgrade"):
            command.downgrade(config, "006_translation_operations")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "013_integrity_receipts"
        )


def test_target_revision_migration_retry_and_evidence_blocks_downgrade(tmp_path):
    database_path = tmp_path / "translator-target-revisions.db"
    config = _migration_config(database_path)
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="migration-test-secret",
    )
    db.init_app(application)

    with application.app_context():
        User.__table__.create(db.engine)
        RuntimeApprovalRequest.__table__.create(db.engine)
        command.upgrade(config, "007_translation_source_plan")
        command.upgrade(config, "021_translation_notes")
        # Alembic upgrade is a retry-safe no-op when the revision is current.
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO translation_target_block_revision
                (id, source_plan_id, block_id, source_plan_hash,
                 structural_fingerprint, contract_version, validator_version,
                 parent_revision_id, parent_revision_hash, author_type, author_user_id,
                 validation_state, target_language, normalized_candidate, rendered_fragment,
                 validation_evidence, source_derived_target_text,
                 candidate_hash, normalized_hash, fragment_hash, evidence_hash, revision_record_hash,
                 created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "target-revision-1",
                "plan-absent-but-evidence-is-durable",
                "block-1",
                "a" * 64,
                "b" * 64,
                "target-block-result-v1",
                "target-block-validator-v1",
                None,
                None,
                "system",
                None,
                "valid",
                "pl",
                "enc:v2:candidate",
                "enc:v2:fragment",
                "enc:v2:evidence",
                "enc:v2:target",
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "0" * 64,
                "2026-08-25T00:00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO translation_window_attempt
                (id, project_id, owner_user_id, operation_id, source_plan_id,
                 source_plan_hash, source_content_hash, operation_version, queue_job_id,
                 operation_lease_expires_at, lease_fence, window_order, packet_json,
                 settings_json, block_ids_json, block_fingerprints_json, predecessor_json, model_profile_json,
                 admission_json, context_manifest_json, packet_hash, prompt_hash, settings_hash,
                 context_hash, context_manifest_hash, model_profile_hash, predecessor_hash,
                 retry_of_attempt_id, retry_of_attempt_hash,
                 rebase_of_attempt_id, rebase_of_attempt_hash,
                 attempt_record_hash, state_head_receipt, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?)
            """,
            (
                "window-attempt-1",
                "project-evidence",
                1,
                "operation-1",
                "plan-absent-but-evidence-is-durable",
                "a" * 64,
                "b" * 64,
                1,
                "legacy-window-attempt-1",
                "2026-08-25T00:00:00",
                "7" * 64,
                1,
                "enc:v2:packet",
                "enc:v2:settings",
                "enc:v2:block-ids",
                "enc:v2:block-fingerprints",
                "enc:v2:predecessor",
                "enc:v2:model",
                "enc:v2:admission",
                "enc:v2:context-manifest",
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "9" * 64,
                "0" * 64,
                None,
                None,
                None,
                None,
                "1" * 64,
                "2" * 64,
                "2026-08-25T00:00:00",
            ),
        )
        connection.commit()

    with application.app_context():
        with pytest.raises(RuntimeError, match="migration 018 cannot downgrade"):
            command.downgrade(config, "007_translation_source_plan")
        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "018_block_workflow_config"
        )


def test_alignment_upgrade_refuses_preexisting_unpaired_active_targets(tmp_path):
    database_path = tmp_path / "translator-alignment-active.db"
    config = _migration_config(database_path)
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="migration-test-secret",
    )
    db.init_app(application)
    with application.app_context():
        User.__table__.create(db.engine)
        RuntimeApprovalRequest.__table__.create(db.engine)
        command.upgrade(config, "009_translation_window_attempts")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        # SQLite FK enforcement is deliberately enabled only in the other
        # regression: this models a real legacy pointer row from Phase 2.
        connection.execute(
            "INSERT INTO translation_target_block_active "
            "(source_plan_id, block_id, revision_id, version, updated_at) "
            "VALUES ('plan-1', 'block-1', 'revision-1', 1, '2026-08-25T00:00:00')"
        )
        connection.commit()
    with application.app_context():
        with pytest.raises(RuntimeError, match="reconciled"):
            command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "009_translation_window_attempts"
        )
        connection.execute("DELETE FROM translation_target_block_active")
        connection.commit()
    with application.app_context():
        command.upgrade(config, "021_translation_notes")
        command.upgrade(config, "021_translation_notes")
        db.session.remove()
        db.engine.dispose()
    with sqlite3.connect(database_path) as connection:
        assert (
            connection.execute(
                "SELECT version_num FROM alembic_version_translator"
            ).fetchone()[0]
            == "021_translation_notes"
        )


def test_phase5a_real_chain_enforces_owner_fks_approval_reuse_and_orm_parity(tmp_path):
    database_path = tmp_path / "translator-phase5-real-chain.db"
    application = _migrate_phase5_database(
        database_path, tmp_path / "instance", revision="head"
    )
    now = "2026-08-25 00:00:00"
    hashes = {"a": "a" * 64, "b": "b" * 64}

    with application.app_context():
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id, email, password_hash) VALUES "
                    "(1, 'one@example.test', 'x'), (2, 'two@example.test', 'x')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_project "
                    "(id, user_id, name, source_lang, target_lang, status, engine_contract) "
                    "VALUES ('project-a', 1, 'A', 'en', 'pl', 'created', 'document-block-v1'), "
                    "('project-b', 2, 'B', 'en', 'pl', 'created', 'document-block-v1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_operation "
                    "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, "
                    "state, source_content_hash, created_at, updated_at) VALUES "
                    "('operation-a', 'project-a', 1, 'document_block_translate', 'a', :fp, "
                    "'running', :hash, :now, :now), "
                    "('operation-b', 'project-b', 2, 'document_block_translate', 'b', :fp2, "
                    "'running', :hash2, :now, :now)"
                ),
                {
                    "fp": hashes["a"],
                    "fp2": hashes["b"],
                    "hash": hashes["a"],
                    "hash2": hashes["b"],
                    "now": now,
                },
            )
            for plan_id, project_id, owner, suffix in (
                ("plan-a", "project-a", 1, "a"),
                ("plan-b", "project-b", 2, "b"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO translation_source_plan "
                        "(id, project_id, contract_version, source_document_id, source_filename, "
                        "source_language, language_profile, namespace, snapshot_contract_version, "
                        "semantic_contract_version, semantic_contract_mode, semantic_content_hash, "
                        "source_content_hash, snapshot_captured_at, planner_version, planner_settings_hash, "
                        "plan_hash, source_metadata, source_attestation, planner_settings, "
                        "source_provenance, coverage_witness, block_count, anchor_count, created_at) "
                        "VALUES (:plan, :project, 'document-block-v1', :doc, 'enc:v2:name', 'en', 'default', "
                        ":namespace, 'snapshot-v1', 'semantic-html-v1', 'strict', :hash, :hash, :now, 'planner-v1', "
                        ":hash, :hash, 'enc:v2:metadata', 'enc:v2:attestation', 'enc:v2:settings', '{}', '{}', 1, 1, :now)"
                    ),
                    {
                        "plan": plan_id,
                        "project": project_id,
                        "doc": f"doc-{suffix}",
                        "namespace": f"ns-{suffix}",
                        "hash": hashes[suffix],
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO translation_block "
                        "(id, source_plan_id, order_index, role, locator, classification_summary, "
                        "source_fragment, source_projection, source_slots, protected_inline, fragment_hash, "
                        "projection_hash, created_at) VALUES (:block, :plan, 1, 'paragraph', '{}', '{}', "
                        "'enc:v2:fragment', 'enc:v2:projection', 'enc:v2:slots', 'enc:v2:protected', :hash, :hash, :now)"
                    ),
                    {
                        "block": f"block-{suffix}",
                        "plan": plan_id,
                        "hash": hashes[suffix],
                        "now": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO translation_source_anchor "
                        "(id, source_plan_id, block_id, order_index, block_order_index, start_offset, end_offset, "
                        "splitter_profile, splitter_version, source_text, source_hash, created_at) VALUES "
                        "(:anchor, :plan, :block, 1, 1, 0, 1, 'unicode', '1', 'enc:v2:source', :hash, :now)"
                    ),
                    {
                        "anchor": f"anchor-{suffix}",
                        "plan": plan_id,
                        "block": f"block-{suffix}",
                        "hash": hashes[suffix],
                        "now": now,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO translation_semantic_census "
                    "(id, project_id, source_plan_id, source_plan_hash, profile_id, profile_version, "
                    "occurrence_count, census_hash, coverage_hash, record_hash, created_at) VALUES "
                    "('census-a', 'project-a', 'plan-a', :ha, 'semantic-census-v1', '1', 0, :ha, :ha, :ha, :now)"
                ),
                {"ha": hashes["a"], "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO translation_context_artifact "
                    "(id, project_id, source_plan_id, owner_user_id, artifact_kind, created_at) VALUES "
                    "('artifact-b', 'project-b', 'plan-b', 2, 'document_brief', :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO translation_context_artifact_revision "
                    "(id, artifact_id, project_id, source_plan_id, revision_number, analysis_state, schema_version, "
                    "profile_id, profile_version, source_plan_hash, schema_hash, profile_hash, model_hash, prompt_hash, "
                    "settings_hash, input_hash, output_hash, evidence_hash, record_hash, model_json, prompt_json, "
                    "settings_json, input_json, output_json, evidence_json, created_at) VALUES "
                    "('artifact-revision-b', 'artifact-b', 'project-b', 'plan-b', 1, 'model', 'context-artifact-v1', "
                    "'brief', '1', :hb, :hb, :hb, :hb, :hb, :hb, :hb, :hb, :hb, :hb, 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', :now)"
                ),
                {"hb": hashes["b"], "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO translation_block_glossary_concept "
                    "(id, project_id, source_plan_id, owner_user_id, concept_scope, created_at) VALUES "
                    "('concept-b', 'project-b', 'plan-b', 2, 'document', :now), "
                    "('concept-a', 'project-a', 'plan-a', 1, 'document', :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO runtime_approval_request "
                    "(id, requester_user_id, approver_user_id, subject_type, subject_id, preview_payload, "
                    "status, created_at, resolved_at) VALUES "
                    "('approval-a', 1, 1, 'translator.glossary-lock.v1', 'glossary-revision-a', '{}', 'approved', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO translation_block_glossary_revision "
                    "(id, concept_id, project_id, source_plan_id, revision_number, approval_request_id, "
                    "approved_by_user_id, approved_at, author_type, revision_state, normalization_profile, "
                    "normalization_version, normalization_hash, keyed_lookup_digest, source_digest, target_digest, "
                    "payload_hash, record_hash, payload_json, created_at) VALUES "
                    "('glossary-revision-b', 'concept-b', 'project-b', 'plan-b', 1, NULL, NULL, NULL, 'model', 'proposal', "
                    "'unicode-casefold-v1', '1', :hb, :hb, :hb, :hb, :hb, :hb, 'enc:v2:{}', :now), "
                    "('glossary-revision-a', 'concept-a', 'project-a', 'plan-a', 1, NULL, NULL, NULL, 'model', 'proposal', "
                    "'unicode-casefold-v1', '1', :ha, :ha, :ha, :ha, :ha, :ha, 'enc:v2:{}', :now)"
                ),
                {"ha": hashes["a"], "hb": hashes["b"], "now": now},
            )
            # Establish one genuinely valid locked revision before probing
            # approval-request reuse and missing-approval FK enforcement.
            connection.execute(
                text(
                    "INSERT INTO translation_block_glossary_revision "
                    "(id, concept_id, project_id, source_plan_id, revision_number, parent_revision_id, "
                    "approval_request_id, approved_by_user_id, approved_at, approval_receipt, author_type, "
                    "revision_state, normalization_profile, normalization_version, normalization_hash, "
                    "keyed_lookup_digest, source_digest, target_digest, payload_hash, record_hash, payload_json, created_at) "
                    "VALUES ('glossary-revision-a-locked', 'concept-a', 'project-a', 'plan-a', 2, "
                    "'glossary-revision-a', 'approval-a', 1, :now, :ha, 'human', 'locked', "
                    "'unicode-casefold-v1', '1', :ha, :ha, :ha, :ha, :ha, :ha, 'enc:v2:{}', :now)"
                ),
                {"ha": hashes["a"], "now": now},
            )
            connection.execute(
                text(
                    "INSERT INTO translation_context_baseline "
                    "(id, operation_id, project_id, owner_user_id, source_plan_id, source_plan_hash, manifest_hash, "
                    "record_hash, binding_attestation, manifest_json, created_at) VALUES "
                    "('baseline-a', 'operation-a', 'project-a', 1, 'plan-a', :ha, :ha, :ha, 'attestation', 'enc:v2:{}', :now)"
                ),
                {"ha": hashes["a"], "now": now},
            )

            def expect_fk(sql: str, params: dict[str, object]) -> None:
                savepoint = connection.begin_nested()
                try:
                    with pytest.raises(IntegrityError):
                        connection.execute(text(sql), params)
                finally:
                    savepoint.rollback()

            savepoint = connection.begin_nested()
            try:
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "UPDATE translation_operation "
                            "SET source_plan_id = 'plan-b', source_plan_hash = :hb "
                            "WHERE id = 'operation-a'"
                        ),
                        {"hb": hashes["b"]},
                    )
            finally:
                savepoint.rollback()

            expect_fk(
                "INSERT INTO translation_operation "
                "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, "
                "state, source_plan_id, source_plan_hash, created_at, updated_at) VALUES "
                "('operation-cross-plan', 'project-a', 1, 'document_block_translate', "
                "'cross-plan', :fp, 'completed', 'plan-b', :hb, :now, :now)",
                {"fp": hashes["a"], "hb": hashes["b"], "now": now},
            )
            # Correcting the project/plan pair must make the same shape valid;
            # otherwise this contract test could be passing on an unrelated
            # NOT NULL or active-operation constraint.
            connection.execute(
                text(
                    "INSERT INTO translation_operation "
                    "(id, project_id, owner_user_id, kind, idempotency_key, request_fingerprint, "
                    "state, source_content_hash, source_plan_id, source_plan_hash, created_at, updated_at) VALUES "
                    "('operation-corrected', 'project-a', 1, 'document_block_translate', "
                    "'corrected', :fp, 'completed', :fp, 'plan-a', :fp, :now, :now)"
                ),
                {"fp": hashes["a"], "now": now},
            )
            expect_fk(
                "INSERT INTO translation_semantic_occurrence "
                "(id, census_id, source_plan_id, block_id, order_index, occurrence_kind, structural_json, value_text, value_hash, occurrence_hash) "
                "VALUES ('occurrence-cross', 'census-a', 'plan-a', 'block-b', 1, 'slot', '{}', 'enc:v2:x', :ha, :ha)",
                {"ha": hashes["a"]},
            )
            expect_fk(
                "INSERT INTO translation_context_artifact_revision "
                "(id, artifact_id, project_id, source_plan_id, revision_number, analysis_state, schema_version, profile_id, profile_version, source_plan_hash, schema_hash, profile_hash, model_hash, prompt_hash, settings_hash, input_hash, output_hash, evidence_hash, record_hash, model_json, prompt_json, settings_json, input_json, output_json, evidence_json, created_at) "
                "VALUES ('artifact-revision-cross', 'artifact-b', 'project-a', 'plan-a', 1, 'model', 'context-artifact-v1', 'brief', '1', :ha, :ha, :ha, :ha, :ha, :ha, :ha, :ha, :ha, :ha, 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', 'enc:v2:{}', :now)",
                {"ha": hashes["a"], "now": now},
            )
            expect_fk(
                "INSERT INTO translation_context_baseline_item "
                "(id, baseline_id, operation_id, project_id, source_plan_id, order_index, item_kind, artifact_revision_id, revision_hash) "
                "VALUES ('baseline-item-cross', 'baseline-a', 'operation-a', 'project-a', 'plan-a', 1, 'document_brief', 'artifact-revision-b', :ha)",
                {"ha": hashes["a"]},
            )
            expect_fk(
                "INSERT INTO translation_context_baseline_item "
                "(id, baseline_id, operation_id, project_id, source_plan_id, order_index, item_kind, glossary_revision_id, revision_hash) "
                "VALUES ('baseline-item-reference-kind', 'baseline-a', 'operation-a', 'project-a', 'plan-a', 1, 'document_brief', 'glossary-revision-a', :ha)",
                {"ha": hashes["a"]},
            )
            expect_fk(
                "INSERT INTO translation_context_baseline_item "
                "(id, baseline_id, operation_id, project_id, source_plan_id, order_index, item_kind, revision_hash) "
                "VALUES ('baseline-item-unrepresentable-kind', 'baseline-a', 'operation-a', 'project-a', 'plan-a', 1, 'research_evidence', :ha)",
                {"ha": hashes["a"]},
            )
            expect_fk(
                "INSERT INTO translation_context_impact "
                "(id, project_id, source_plan_id, block_id, old_glossary_revision_id, reason_json, impact_state, created_at) "
                "VALUES ('impact-cross', 'project-a', 'plan-a', 'block-a', 'glossary-revision-b', 'enc:v2:{}', 'needs_review', :now)",
                {"now": now},
            )
            expect_fk(
                "INSERT INTO translation_block_glossary_revision "
                "(id, concept_id, project_id, source_plan_id, revision_number, parent_revision_id, approval_request_id, approved_by_user_id, approved_at, approval_receipt, author_type, revision_state, normalization_profile, normalization_version, normalization_hash, keyed_lookup_digest, source_digest, target_digest, payload_hash, record_hash, payload_json, created_at) "
                "VALUES ('glossary-revision-invalid-approval', 'concept-a', 'project-a', 'plan-a', 3, 'glossary-revision-a', 'missing-approval', 1, :now, :ha, 'human', 'locked', 'unicode-casefold-v1', '1', :ha, :ha, :ha, :ha, :ha, :ha, 'enc:v2:{}', :now)",
                {"ha": hashes["a"], "now": now},
            )
            savepoint = connection.begin_nested()
            try:
                with pytest.raises(IntegrityError):
                    connection.execute(
                        text(
                            "INSERT INTO translation_block_glossary_revision "
                            "(id, concept_id, project_id, source_plan_id, revision_number, parent_revision_id, approval_request_id, approved_by_user_id, approved_at, approval_receipt, author_type, revision_state, normalization_profile, normalization_version, normalization_hash, keyed_lookup_digest, source_digest, target_digest, payload_hash, record_hash, payload_json, created_at) "
                            "VALUES ('glossary-revision-reused', 'concept-a', 'project-a', 'plan-a', 3, 'glossary-revision-a', 'approval-a', 1, :now, :ha, 'human', 'locked', 'unicode-casefold-v1', '1', :ha, :ha, :ha, :ha, :ha, :ha, 'enc:v2:{}', :now)"
                        ),
                        {"ha": hashes["a"], "now": now},
                    )
            finally:
                savepoint.rollback()

        inspector = sqlalchemy_inspect(db.engine)
        phase5_tables = (
            "translation_source_anchor",
            "translation_semantic_census",
            "translation_semantic_occurrence",
            "translation_context_artifact",
            "translation_context_artifact_revision",
            "translation_block_glossary_concept",
            "translation_block_glossary_revision",
            "translation_block_glossary_active",
            "translation_block_glossary_occurrence",
            "translation_context_baseline",
            "translation_context_baseline_item",
            "translation_context_impact",
            "translation_embedding_generation",
            "translation_embedding_entry",
            "translation_embedding_active",
            "translation_operation_evidence_reuse",
            "translation_source_plan_active",
            "translation_source_plan_correction",
        )
        for table_name in phase5_tables:
            table = db.metadata.tables[table_name]
            assert [(column.name, column.nullable) for column in table.columns] == [
                (column["name"], column["nullable"])
                for column in inspector.get_columns(table_name)
            ]
            model_unique = sorted(
                (constraint.name, tuple(column.name for column in constraint.columns))
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
                and constraint.name is not None
            )
            db_unique = sorted(
                (constraint["name"], tuple(constraint["column_names"]))
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint["name"] is not None
            )
            assert model_unique == db_unique
            model_indexes = sorted(
                (
                    index.name,
                    tuple(column.name for column in index.columns),
                    index.unique,
                )
                for index in table.indexes
            )
            db_indexes = sorted(
                (index["name"], tuple(index["column_names"]), bool(index["unique"]))
                for index in inspector.get_indexes(table_name)
            )
            assert model_indexes == db_indexes
            model_fks = sorted(
                (
                    constraint.name,
                    tuple(column.name for column in constraint.columns),
                    tuple(element.target_fullname for element in constraint.elements),
                )
                for constraint in table.constraints
                if constraint.__class__.__name__ == "ForeignKeyConstraint"
            )
            db_fks = sorted(
                (
                    fk["name"],
                    tuple(fk["constrained_columns"]),
                    tuple(
                        f"{fk['referred_table']}.{column}"
                        for column in fk["referred_columns"]
                    ),
                )
                for fk in inspector.get_foreign_keys(table_name)
            )
            assert model_fks == db_fks
            assert {
                check["name"] for check in inspector.get_check_constraints(table_name)
            } == {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
                and constraint.name is not None
            }
        db.session.remove()
        db.engine.dispose()
