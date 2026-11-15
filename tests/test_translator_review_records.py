from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import text
from test_translator_migrations import _migrate_phase5_database, _migration_config
from test_translator_review_coverage import _activate_target, _project_and_plan
from test_translator_review_coverage import app as fixture_app  # noqa: F401

from iarf_assistant.addons.translator.document_review import review_fetch
from iarf_assistant.addons.translator.review_records import (
    create_flag,
    list_flags,
    transition_flag,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
)
from iarf_assistant.extensions import db

_REVIEW_RECORD_TABLES = {
    "translation_target_edit_reason",
    "translation_review_flag_event",
    "translation_qa_disposition",
}


def test_migration_030_is_additive_and_refuses_review_record_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "review-records-migration.db"
    config = _migration_config(database_path)
    application = _migrate_phase5_database(
        database_path,
        tmp_path / "review-records-migration-instance",
        revision="029_epub_editions",
    )
    with application.app_context():
        command.upgrade(config, "030_review_records")
        tables = set(sqlalchemy_inspect(db.engine).get_table_names())
        assert tables >= _REVIEW_RECORD_TABLES
        command.upgrade(config, "030_review_records")
        command.downgrade(config, "029_epub_editions")
        assert not _REVIEW_RECORD_TABLES & set(
            sqlalchemy_inspect(db.engine).get_table_names()
        )

        command.upgrade(config, "030_review_records")
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO translation_review_flag_event "
                    "(id, flag_id, sequence, project_id, source_plan_id, block_id, "
                    "owner_user_id, action, kind, origin, actor_type, payload_json, "
                    "record_hash, integrity_receipt, created_at) VALUES "
                    "('flag-row', 'flag-row', 1, 'project', 'plan', 'block', 1, "
                    "'open', 'assumption', 'agent', 'owner', 'encrypted', "
                    ":record_hash, :receipt, CURRENT_TIMESTAMP)"
                ),
                {"record_hash": "r" * 64, "receipt": "i" * 64},
            )
        with pytest.raises(RuntimeError, match="cannot downgrade review records"):
            command.downgrade(config, "029_epub_editions")
        assert (
            db.session.execute(
                text("SELECT version_num FROM alembic_version_translator")
            ).scalar_one()
            == "030_review_records"
        )
        with db.engine.begin() as connection, pytest.raises(Exception, match="CHECK"):
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    "INSERT INTO translation_review_flag_event "
                    "(id, flag_id, sequence, project_id, source_plan_id, block_id, "
                    "owner_user_id, action, kind, origin, actor_type, payload_json, "
                    "record_hash, integrity_receipt, created_at) VALUES "
                    "('flag-row-2', 'flag-row', 2, 'project', 'plan', 'block', 1, "
                    "'open', 'assumption', 'agent', 'owner', 'encrypted', "
                    ":record_hash, :receipt, CURRENT_TIMESTAMP)"
                ),
                {"record_hash": "r" * 64, "receipt": "i" * 64},
            )
        db.session.remove()
        db.engine.dispose()


def _flag_scope(name: str):
    project, _record, blocks, owner = _project_and_plan(name)
    block = blocks[0]
    _activate_target(project, block)
    db.session.commit()
    return project, block, ReviewActor("owner", owner.id)


def _review_error(call, *args, **kwargs) -> str:
    with pytest.raises(ScholarlyReviewError) as raised:
        call(*args, **kwargs)
    db.session.rollback()
    return raised.value.code


@pytest.mark.usefixtures("fixture_app")
def test_flags_are_encrypted_replayable_and_fenced() -> None:
    project, block, actor = _flag_scope("Flags")
    request = {
        "block_id": block.id,
        "kind": "assumption",
        "body": "Read the term as parish, not municipality.",
        "excerpt": "source sentence",
        "actor": actor,
        "idempotency_key": "flag-parish",
    }
    opened = create_flag(project, **request)
    db.session.commit()
    assert opened["state"] == "open"
    assert opened["sequence"] == 1
    assert opened["origin"] == "agent"
    assert opened["target_version"] == 1
    assert opened["excerpt"] == "source sentence"
    assert create_flag(project, **request) == opened
    assert (
        _review_error(create_flag, project, **{**request, "body": "Something else."})
        == "review-flag-idempotency-conflict"
    )
    stored = db.session.execute(
        text("SELECT payload_json FROM translation_review_flag_event")
    ).scalar_one()
    assert "parish" not in stored

    listed = list_flags(project, actor=actor)
    assert [item["id"] for item in listed["items"]] == [opened["id"]]
    assert listed["summary"]["open"] == 1
    assert listed["summary"]["open_by_kind"]["assumption"] == 1
    bilingual = review_fetch(project, block_ids=[block.id])
    assert [flag["id"] for flag in bilingual["items"][0]["flags"]] == [opened["id"]]
    target_only = review_fetch(project, block_ids=[block.id], view="target_only")
    assert "flags" not in target_only["items"][0]

    transition = {
        "flag_id": opened["id"],
        "action": "resolve",
        "expected_sequence": 1,
        "actor": actor,
        "idempotency_key": "resolve-parish",
        "note": "Confirmed by the surrounding chapter.",
    }
    assert (
        _review_error(transition_flag, project, **{**transition, "note": None})
        == "review-flag-note"
    )
    assert (
        _review_error(
            transition_flag, project, **{**transition, "expected_sequence": 2}
        )
        == "review-flag-version-conflict"
    )
    resolved = transition_flag(project, **transition)
    db.session.commit()
    assert (resolved["state"], resolved["sequence"]) == ("resolved", 2)
    assert resolved["history"][0]["note"] == "Confirmed by the surrounding chapter."
    assert transition_flag(project, **transition) == resolved
    assert (
        _review_error(
            transition_flag,
            project,
            **{**transition, "expected_sequence": 2, "idempotency_key": "again"},
        )
        == "review-flag-transition"
    )
    assert list_flags(project, actor=actor)["items"] == []
    assert (
        list_flags(project, actor=actor, states=["resolved"])["pagination"]["total"]
        == 1
    )
    assert review_fetch(project, block_ids=[block.id])["items"][0]["flags"] == []

    reopened = transition_flag(
        project,
        flag_id=opened["id"],
        action="reopen",
        expected_sequence=2,
        actor=actor,
        idempotency_key="reopen-parish",
    )
    db.session.commit()
    assert (reopened["state"], reopened["sequence"]) == ("open", 3)
    assert [event["action"] for event in reopened["history"]] == ["resolve", "reopen"]


@pytest.mark.usefixtures("fixture_app")
def test_flags_validate_scope_authority_and_integrity() -> None:
    project, block, actor = _flag_scope("FlagChecks")
    base = {
        "block_id": block.id,
        "kind": "unverified",
        "body": "Date not checked against the archive.",
        "actor": actor,
        "idempotency_key": "flag-date",
    }
    assert _review_error(create_flag, project, **{**base, "kind": "mood"}) == (
        "review-flag-kind"
    )
    assert _review_error(create_flag, project, **{**base, "block_id": "nope"}) == (
        "review-flag-block"
    )
    assert _review_error(create_flag, project, **{**base, "body": "  "}) == (
        "review-flag-body"
    )
    stranger = ReviewActor("owner", 999_999)
    assert _review_error(create_flag, project, **{**base, "actor": stranger}) == (
        "review-mutation-forbidden"
    )
    assert _review_error(list_flags, project, actor=stranger) == "review-read-forbidden"
    assert (
        _review_error(
            transition_flag,
            project,
            flag_id="missing",
            action="dismiss",
            expected_sequence=1,
            actor=actor,
            idempotency_key="dismiss-missing",
            note="Not needed.",
        )
        == "review-flag-missing"
    )
    assert _review_error(list_flags, project, actor=actor, limit=0) == (
        "review-flag-limit"
    )

    create_flag(project, **base)
    db.session.commit()
    db.session.execute(
        text("UPDATE translation_review_flag_event SET kind = 'revisit'")
    )
    db.session.commit()
    db.session.expire_all()
    assert _review_error(list_flags, project, actor=actor) == "review-flag-integrity"
