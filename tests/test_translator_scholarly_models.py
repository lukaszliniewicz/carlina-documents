from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_translator_migrations import _migrate_phase5_database, _migration_config

from iarf_assistant.addons.translator.models import (  # noqa: F401
    TranslationBlockReviewHead,
    TranslationBlockReviewRevision,
    TranslationContextBaselineItem,
    TranslationGlossarySuggestion,
    TranslationReaderNote,
    TranslationReaderNoteHead,
    TranslationReaderNoteRevision,
    TranslationResearchDossier,
    TranslationResearchDossierGlossaryProposal,
    TranslationResearchDossierRevision,
    TranslationRetranslationRequest,
    TranslationShareGrant,
    TranslationShareSession,
    TranslationTargetRevisionShareAttribution,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db

NEW_TABLES = {
    "translation_research_dossier",
    "translation_research_dossier_revision",
    "translation_research_dossier_glossary_proposal",
    "translation_block_glossary_suggestion",
    "translation_share_grant",
    "translation_share_session",
    "translation_reader_note",
    "translation_reader_note_revision",
    "translation_reader_note_head",
    "translation_block_review_revision",
    "translation_block_review_head",
    "translation_retranslation_request",
    "translation_target_revision_share_attribution",
}


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'scholarly.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="scholarly-model-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    db.create_all()
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


def test_metadata_has_scholarly_tables_and_encrypted_secret_surfaces(app) -> None:
    inspector = sqlalchemy_inspect(db.engine)
    assert NEW_TABLES <= set(inspector.get_table_names())
    grant_columns = {column["name"] for column in inspector.get_columns("translation_share_grant")}
    assert "token" not in grant_columns
    assert "raw_token" not in grant_columns
    assert "token_digest" in grant_columns

    baseline_columns = {
        column["name"]
        for column in inspector.get_columns("translation_context_baseline_item")
    }
    assert "research_dossier_revision_id" in baseline_columns
    for table in (
        "translation_research_dossier_revision",
        "translation_reader_note_revision",
        "translation_block_review_revision",
        "translation_target_revision_share_attribution",
    ):
        assert "integrity_receipt" in {
            column["name"] for column in inspector.get_columns(table)
        }
    request_uniques = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "translation_retranslation_request"
        )
    }
    assert "uq_translation_retranslation_request_operation" in request_uniques
    request_checks = {
        constraint["name"]: constraint["sqltext"]
        for constraint in inspector.get_check_constraints(
            "translation_retranslation_request"
        )
    }
    assert "fresh_scope" not in request_checks[
        "ck_translation_retranslation_request_research_choice"
    ]


def test_new_check_constraints_reject_invalid_shapes(app) -> None:
    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO translation_share_grant "
                "(id, project_id, owner_user_id, label, role, token_digest, "
                "token_hint, status, created_at) VALUES "
                "('grant', 'missing-project', 99, 'share', 'admin', :digest, 'hint', "
                "'active', CURRENT_TIMESTAMP)"
            ),
            {"digest": "d" * 128},
        )
    db.session.rollback()

    with pytest.raises(IntegrityError):
        db.session.execute(
            text(
                "INSERT INTO translation_reader_note_revision "
                "(id, note_id, project_id, source_plan_id, block_id, revision_number, "
                "state, anchor_kind, target_revision_id, occurrence_index, body, "
                "citations_json, author_type, record_hash, created_at) VALUES "
                "('revision', 'note', 'project', 'plan', 'block', 1, 'proposed', "
                "'block', 'target', 1, 'body', '[]', 'system', :record_hash, CURRENT_TIMESTAMP)"
            ),
            {"record_hash": "r" * 64},
        )
    db.session.rollback()


def test_migration_024_is_additive_and_cleanly_reversible(tmp_path: Path) -> None:
    database_path = tmp_path / "scholarly-migration.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "migration-instance",
        revision="023_passive_window_pending",
    )

    with application.app_context():
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user (id, email, password_hash) "
                    "VALUES (1, 'scholarly@example.test', 'x')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO translation_project "
                    "(id, user_id, name, source_lang, target_lang, status) "
                    "VALUES ('scholarly-project', 1, 'Book', 'en', 'pl', 'created')"
                )
            )

        command.upgrade(config, "head")
        assert db.session.execute(
            text("SELECT id FROM translation_project")
        ).scalar_one() == "scholarly-project"
        assert NEW_TABLES <= set(sqlalchemy_inspect(db.engine).get_table_names())

        command.downgrade(config, "023_passive_window_pending")
        assert db.session.execute(
            text("SELECT id FROM translation_project")
        ).scalar_one() == "scholarly-project"
        columns = {
            column["name"]
            for column in sqlalchemy_inspect(db.engine).get_columns(
                "translation_context_baseline_item"
            )
        }
        assert "research_dossier_revision_id" not in columns

        db.session.remove()
        db.engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version_num FROM alembic_version_translator"
        ).fetchone()[0] == "023_passive_window_pending"


def test_migration_024_refuses_to_discard_reviewer_target_attribution(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "scholarly-reviewer-downgrade.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "reviewer-downgrade-instance",
        revision="023_passive_window_pending",
    )

    with application.app_context():
        command.upgrade(config, "head")
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """
                INSERT INTO translation_target_block_revision
                    (id, source_plan_id, block_id, source_plan_hash,
                     structural_fingerprint, contract_version, validator_version,
                     parent_revision_id, parent_revision_hash, author_type,
                     author_user_id, validation_state, target_language,
                     normalized_candidate, rendered_fragment, validation_evidence,
                     source_derived_target_text, candidate_hash, normalized_hash,
                     fragment_hash, evidence_hash, revision_record_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "reviewer-target-revision",
                    "reviewer-plan",
                    "reviewer-block",
                    "a" * 64,
                    "b" * 64,
                    "target-block-result-v1",
                    "target-block-validator-v1",
                    None,
                    None,
                    "reviewer",
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
                    "2026-09-05T00:00:00",
                ),
            )
            connection.commit()

        with pytest.raises(RuntimeError, match="reviewer-authored target revisions"):
            command.downgrade(config, "023_passive_window_pending")
        assert db.session.execute(
            text("SELECT version_num FROM alembic_version_translator")
        ).scalar_one() == "024_scholarly_review"
        assert "translation_target_revision_share_attribution" in set(
            sqlalchemy_inspect(db.engine).get_table_names()
        )

        db.session.remove()
        db.engine.dispose()
