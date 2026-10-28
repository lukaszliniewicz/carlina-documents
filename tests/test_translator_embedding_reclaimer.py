"""Focused safety tests for Translator embedding vector reclamation."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from iarf_assistant.addons import translator

routes = import_module(f"{translator.__name__}.routes")
_project_deletion = import_module(f"{translator.__name__}.project_deletion")
_embedding_reclaimer = import_module(f"{translator.__name__}.embedding_reclaimer")
cleanup_embedding_vector_identities = (
    _embedding_reclaimer.cleanup_embedding_vector_identities
)
reconcile_embedding_vector_tables = (
    _embedding_reclaimer.reconcile_embedding_vector_tables
)
db = translator.db


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__)
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'reclaimer.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


def _table(application: Flask, name: str) -> None:
    with application.app_context(), db.engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE "{name}" (rowid INTEGER)'))


def _table_names(application: Flask) -> set[str]:
    with application.app_context(), db.engine.connect() as connection:
        return set(
            connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars()
        )


def _authority(application: Flask, rows: list[tuple[str, int, str]]) -> None:
    with application.app_context(), db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE translation_embedding_generation "
                "(namespace TEXT, dimensions INTEGER, status TEXT)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO translation_embedding_generation "
                "(namespace, dimensions, status) VALUES (:namespace, :dimensions, :status)"
            ),
            [
                {"namespace": namespace, "dimensions": dimensions, "status": status}
                for namespace, dimensions, status in rows
            ],
        )


def test_reconciler_allowlist_preserves_shadow_legacy_and_foreign_tables(app):
    _authority(app, [("translator_emb_live", 4, "active")])
    for name in (
        "vec_translator_emb_live_4",
        "vec_translator_emb_retired_8",
        "vec_translator_emb_orphan_3",
        "vec_translator_emb_retired_8_rowid",
        "vec_translator_tm_legacy_4",
        "vec_foreign_4",
        "vec_translator_emb_bad_0",
    ):
        _table(app, name)

    report = reconcile_embedding_vector_tables(app)
    names = _table_names(app)

    assert report.authority_available is True
    assert report.dropped == (
        ("translator_emb_orphan", 3),
        ("translator_emb_retired", 8),
    )
    assert "vec_translator_emb_live_4" in names
    assert "vec_translator_emb_retired_8" not in names
    assert "vec_translator_emb_orphan_3" not in names
    assert "vec_translator_emb_retired_8_rowid" in names
    assert "vec_translator_tm_legacy_4" in names
    assert "vec_foreign_4" in names
    assert "vec_translator_emb_bad_0" in names


def test_reconciler_keeps_building_ready_and_active_and_is_idempotent(app):
    rows = [
        ("translator_emb_building", 2, "building"),
        ("translator_emb_ready", 3, "ready"),
        ("translator_emb_active", 4, "active"),
        ("translator_emb_failed", 5, "failed"),
        ("translator_emb_retired", 6, "retired"),
    ]
    _authority(app, rows)
    for namespace, dimensions, _status in rows:
        _table(app, f"vec_{namespace}_{dimensions}")

    first = reconcile_embedding_vector_tables(app)
    second = reconcile_embedding_vector_tables(app)

    assert set(first.kept) == {
        ("translator_emb_building", 2),
        ("translator_emb_ready", 3),
        ("translator_emb_active", 4),
    }
    assert set(first.dropped) == {
        ("translator_emb_failed", 5),
        ("translator_emb_retired", 6),
    }
    assert second.dropped == ()


def test_missing_authority_table_fails_safe(app):
    _table(app, "vec_translator_emb_orphan_3")
    _table(app, "vec_translator_emb_other_5")

    report = reconcile_embedding_vector_tables(app)

    assert report.authority_available is False
    assert report.dropped == ()
    assert "vec_translator_emb_orphan_3" in _table_names(app)
    assert "vec_translator_emb_other_5" in _table_names(app)


def test_unreadable_authority_table_fails_safe(app):
    _table(app, "vec_translator_emb_orphan_3")
    with app.app_context(), db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE translation_embedding_generation "
                "(namespace TEXT, dimensions INTEGER)"
            )
        )

    report = reconcile_embedding_vector_tables(app)

    assert report.authority_available is False
    assert report.dropped == ()
    assert "vec_translator_emb_orphan_3" in _table_names(app)


def test_exact_cleanup_validates_identities_and_is_retryable(app):
    _authority(app, [("translator_emb_other", 5, "active")])
    for name in (
        "vec_translator_emb_project_4",
        "vec_translator_emb_other_5",
        "vec_translator_emb_project_4_shadow",
        "vec_translator_tm_legacy_4",
    ):
        _table(app, name)

    report = cleanup_embedding_vector_identities(
        app,
        [
            ("translator_emb_project", 4),
            ("translator_emb_other", 5),
            ("translator_emb_project", "004"),
            ("translator_emb_project;DROP TABLE x", 4),
        ],
    )
    retry = cleanup_embedding_vector_identities(
        app, [("translator_emb_project", 4)]
    )

    assert report.dropped == (("translator_emb_project", 4),)
    assert report.kept == (("translator_emb_other", 5),)
    assert retry.dropped == ()
    names = _table_names(app)
    assert "vec_translator_emb_project_4_shadow" in names
    assert "vec_translator_emb_other_5" in names
    assert "vec_translator_tm_legacy_4" in names


def test_exact_cleanup_fails_safe_without_relational_authority(app):
    _table(app, "vec_translator_emb_project_4")

    report = cleanup_embedding_vector_identities(
        app, [("translator_emb_project", 4)]
    )

    assert report.authority_available is False
    assert report.dropped == ()
    assert "vec_translator_emb_project_4" in _table_names(app)


def test_delete_project_collects_embedding_identities_before_commit_and_cleans_after_commit():
    application = Flask(__name__)
    bind_content_policy(application)
    application.secret_key = "test"
    application.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(application)
    project = SimpleNamespace(
        id="project-1",
        user_id=7,
        source_lang="en",
        target_lang="pl",
        segments=[],
    )
    generations = [
        SimpleNamespace(namespace="translator_emb_project", dimensions=4),
        SimpleNamespace(namespace="translator_emb_project", dimensions=8),
    ]
    events: list[Any] = []

    class Query:
        def filter_by(self, **kwargs):
            assert kwargs == {"id": "project-1", "user_id": 7}
            return self

        def all(self):
            return generations

        def first(self):
            return project

    class GenerationQuery:
        def filter_by(self, **kwargs):
            assert kwargs == {"project_id": "project-1"}
            return self

        def all(self):
            events.append("collect")
            return generations

    def execute(statement, *_args, **_kwargs):
        if "DELETE FROM translation_project" in str(statement):
            events.append("delete")

    with application.test_request_context(
        "/projects/project-1", method="DELETE"
    ), patch.object(routes, "current_user", SimpleNamespace(id=7)), patch.object(
        routes.TranslationProject, "query", Query()
    ), patch.object(
        _project_deletion.TranslationEmbeddingGeneration, "query", GenerationQuery()
    ), patch.object(
        routes, "reconcile_expired_active_operation", return_value=None
    ), patch.object(
        _project_deletion,
        "inspect",
        return_value=SimpleNamespace(has_table=lambda _name: True),
    ), patch.object(
        routes.db.session,
        "execute",
        side_effect=execute,
    ), patch.object(
        routes.db.session,
        "commit",
        side_effect=lambda: events.append("commit"),
    ), patch.object(
        _project_deletion,
        "cleanup_embedding_vector_identities",
        side_effect=lambda _app, identities: events.append(
            ("cleanup", list(identities))
        ),
    ):
        response = routes.delete_project.__wrapped__("project-1")

    assert response.get_json() == {"success": True}
    assert events == [
        "collect",
        "delete",
        "commit",
        (
            "cleanup",
            [("translator_emb_project", 4), ("translator_emb_project", 8)],
        ),
    ]


def test_translator_jobs_register_reconciliation_only_in_scheduler_process():
    application = Flask(__name__)
    bind_content_policy(application)
    calls = []
    context = SimpleNamespace(
        application=application,
        ensure_worker_pool=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    addon = translator.TranslatorAddon.__new__(translator.TranslatorAddon)
    object.__setattr__(addon, "context", context)
    addon.id = "translator"

    class Scheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append((func, trigger, kwargs))

    scheduler = Scheduler()
    with patch.object(translator, "is_worker_process", return_value=False):
        addon.on_register_jobs(scheduler)

    assert len(calls) == 1
    embedding_jobs = [
        job for job in scheduler.jobs if job[0] is reconcile_embedding_vector_tables
    ]
    assert [trigger for _func, trigger, _kwargs in embedding_jobs] == [
        "date",
        "interval",
    ]
    assert {job[2]["id"] for job in embedding_jobs} == {
        "translator_embedding_reconcile_startup",
        "translator_embedding_reconcile_periodic",
    }
    assert {job[2]["id"] for job in scheduler.jobs} == {
        "translator_embedding_reconcile_startup",
        "translator_embedding_reconcile_periodic",
        "translator_operation_lease_reap_startup",
        "translator_operation_lease_reap_periodic",
    }
    assert all(job[2]["max_instances"] == 1 for job in scheduler.jobs)
    assert all(job[2]["coalesce"] is True for job in scheduler.jobs)
    assert embedding_jobs[1][2]["hours"] == 1

    worker_scheduler = Scheduler()
    with patch.object(translator, "is_worker_process", return_value=True):
        addon.on_register_jobs(worker_scheduler)
    assert calls == [(('worker.py',), {"num_workers": 3})]
    assert worker_scheduler.jobs == []
