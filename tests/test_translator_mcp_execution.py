"""Durability and call contracts for Translator-owned MCP admission."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.translator.mcp_execution import (
    create_mcp_project,
    start_mcp_execution,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.executor_profile import require_executor_profile
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-mcp.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="synthetic-translator-mcp-secret",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with application.app_context():
        with db.engine.connect() as connection:
            assert (
                connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() == "wal"
            )
            connection.commit()
        db.create_all()
        db.session.add_all(
            [
                User(id=713, email="owner@example.test", password_hash="x"),
                User(id=714, email="other@example.test", password_hash="x"),
            ]
        )
        db.session.commit()
        try:
            yield application
        finally:
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _rows(app: Flask) -> list[tuple[Any, ...]]:
    database_path = app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
    with closing(sqlite3.connect(database_path, timeout=0.1)) as connection:
        return connection.execute(
            "SELECT id, owner_user_id, resource_id, queue_job_id, mode, status "
            "FROM mcp_execution ORDER BY owner_user_id, id"
        ).fetchall()


class StartupClient:
    def __init__(self, *, fail_at: str | None = None, job_id: str | None = "queue-1"):
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_at = fail_at
        self.job_id = job_id

    def set_mcp_execution(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("set", args, kwargs))
        if self.fail_at == "set":
            raise ValueError("synthetic set failure")

    def start_block_translation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", args, kwargs))
        if self.fail_at == "start":
            raise ValueError("synthetic start failure")
        return {"job_id": self.job_id}

    def mcp_job_projection(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("project", args, kwargs))
        if self.fail_at == "project":
            raise ValueError("synthetic project projection failure")
        return {"id": args[0], "status": "running"}


def _start(
    *,
    client_factory: Callable[[], StartupClient],
    execution_payload: Callable[[Any], dict[str, Any]] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return start_mcp_execution(
        owner_user_id=713,
        project_id="project-1",
        mode="passive",
        client_factory=client_factory,
        execution_payload=execution_payload
        or (lambda execution: {"id": execution.id, "status": execution.status}),
        **kwargs,
    )


def test_start_commits_before_factory_and_preserves_calls_and_projection(app: Flask):
    client = StartupClient()
    observed: list[tuple[Any, ...]] = []
    profile = require_executor_profile(
        {
            "schema_version": "carlina.external-executor.v1",
            "max_input_tokens": 32_000,
            "max_output_tokens": 4_000,
            "input_modalities": ["text"],
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_reasoning": True,
            "user_confirmed": True,
        }
    )

    def factory() -> StartupClient:
        database_path = app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
        with closing(sqlite3.connect(database_path, timeout=0.1)) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            connection.execute("BEGIN IMMEDIATE")
            observed.extend(
                connection.execute(
                    "SELECT owner_user_id, addon_id, resource_kind, resource_id, "
                    "queue_job_id, mode, status FROM mcp_execution"
                ).fetchall()
            )
            connection.rollback()
        return client

    result = _start(
        client_factory=factory,
        executor_profile=profile,
        research_dossier_revision_id="dossier-r1",
        context_mode="retained",
    )

    assert len(observed) == 1
    assert observed[0][:7] == (
        713,
        "translator",
        "project",
        "project-1",
        None,
        "passive",
        "queued",
    )
    execution = db.session.get(McpExecution, _rows(app)[0][0])
    assert execution is not None
    assert execution.request_json == {
        "project_id": "project-1",
        "mode": "passive",
        "executor_profile": profile.to_dict(),
        "research_dossier_revision_id": "dossier-r1",
        "context_mode": "retained",
    }
    assert result == {
        "execution": {"id": _rows(app)[0][0], "status": "queued"},
        "project": {"id": "project-1", "status": "running"},
    }
    assert client.calls == [
        (
            "set",
            ("project-1", result["execution"]["id"]),
            {"mode": "passive", "executor_profile": profile},
        ),
        (
            "start",
            ("project-1",),
            {
                "executor_profile": profile,
                "research_dossier_revision_id": "dossier-r1",
                "context_mode": "retained",
            },
        ),
        ("project", ("project-1",), {"execution_id": result["execution"]["id"]}),
    ]
    assert _rows(app)[0][3:] == ("queue-1", "passive", "queued")


@pytest.mark.parametrize("failure", ["factory", "set", "start", "empty_job"])
def test_start_failure_is_independently_committed_as_failed_unbound(
    app: Flask, failure: str
):
    client = StartupClient(
        fail_at=failure if failure in {"set", "start"} else None,
        job_id=None if failure == "empty_job" else "queue-1",
    )

    def factory() -> StartupClient:
        if failure == "factory":
            raise ValueError("synthetic factory failure")
        return client

    expected = (
        "Translator did not return a durable queue job."
        if failure == "empty_job"
        else f"synthetic {failure} failure"
    )
    with pytest.raises((RuntimeError, ValueError), match=expected):
        _start(client_factory=factory)

    rows = _rows(app)
    assert len(rows) == 1
    assert rows[0][1:] == (713, "project-1", None, "passive", "failed")


@pytest.mark.parametrize("failure", ["execution", "project"])
def test_projection_failure_retains_committed_queued_binding(app: Flask, failure: str):
    client = StartupClient(fail_at="project" if failure == "project" else None)

    def serialize(_execution: Any) -> dict[str, Any]:
        if failure == "execution":
            raise ValueError("synthetic execution projection failure")
        return {"ok": True}

    with pytest.raises(ValueError, match=f"synthetic {failure} projection failure"):
        _start(client_factory=lambda: client, execution_payload=serialize)

    rows = _rows(app)
    assert len(rows) == 1
    assert rows[0][1:] == (713, "project-1", "queue-1", "passive", "queued")


class CreateClient:
    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def create_from_document(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((args, kwargs))
        return self.result


@pytest.mark.parametrize(
    ("mode", "settings", "expected_pre_analysis"),
    [
        ("passive", {"custom": "value"}, False),
        ("passive", {"enable_pre_analysis": True}, True),
        ("managed", {"custom": "value"}, None),
    ],
)
def test_create_preserves_settings_and_exact_deferred_call(
    app: Flask,
    mode: str,
    settings: dict[str, Any],
    expected_pre_analysis: bool | None,
):
    original = dict(settings)
    created = {
        "id": "project-created",
        "status": "planning",
        "operation": {"id": "fallback-operation"},
    }
    client = CreateClient(created)
    result = create_mcp_project(
        owner_user_id=713,
        document_id="document-1",
        name="Synthetic project",
        source_lang="de",
        target_lang="en",
        mode=mode,
        idempotency_key="stable-key",
        settings=settings,
        client_factory=lambda: client,
        execution_payload=lambda execution: {"id": execution.id},
    )

    assert settings == original
    assert result == {
        "project_id": "project-created",
        "project": created,
        "status": "planning",
        "job_id": None,
        "operation": {"id": "fallback-operation"},
    }
    assert len(client.calls) == 1
    args, kwargs = client.calls[0]
    assert args == ("document-1",)
    assert kwargs == {
        "name": "Synthetic project",
        "source_lang": "de",
        "target_lang": "en",
        "idempotency_key": "stable-key",
        "defer_source_planning": True,
        **original,
        "execution_mode": mode,
        **(
            {"enable_pre_analysis": expected_pre_analysis}
            if expected_pre_analysis is not None
            else {}
        ),
    }
    assert _rows(app) == []


def test_create_ingestion_deduplicates_per_owner_and_queue(app: Flask):
    created = {
        "id": "project-created",
        "status": "planning",
        "job_id": 42,
        "latest_operation": {"id": "latest-operation"},
        "operation": {"id": "older-operation"},
    }
    client = CreateClient(created)

    def create(owner_user_id: int) -> dict[str, Any]:
        return create_mcp_project(
            owner_user_id=owner_user_id,
            document_id="document-1",
            name="Synthetic project",
            source_lang="de",
            target_lang="en",
            mode="passive",
            idempotency_key="stable-key",
            settings={},
            client_factory=lambda: client,
            execution_payload=lambda execution: {
                "id": execution.id,
                "owner": execution.owner_user_id,
                "queue": execution.queue_job_id,
                "request": execution.request_json,
            },
        )

    first = create(713)
    retry = create(713)
    other_owner = create(714)
    created["job_id"] = 43
    other_queue = create(713)

    assert first["operation"] == {"id": "latest-operation"}
    assert first["ingestion_execution"] == retry["ingestion_execution"]
    assert first["ingestion_execution"] != other_owner["ingestion_execution"]
    assert first["ingestion_execution"] != other_queue["ingestion_execution"]
    assert first["ingestion_execution"]["queue"] == "42"
    assert first["ingestion_execution"]["request"] == {"kind": "source_ingestion"}
    rows = _rows(app)
    assert len(rows) == 3
    assert {row[1:] for row in rows} == {
        (713, "project-created", "42", "passive", "queued"),
        (713, "project-created", "43", "passive", "queued"),
        (714, "project-created", "42", "passive", "queued"),
    }
