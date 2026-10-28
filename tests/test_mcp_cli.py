from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from iarf_assistant.cli import _register_mcp_cli
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    MCP_REQUEST_TOMBSTONE,
    McpAuthError,
    create_execution,
    get_or_create_work,
    grant_access_key_scopes,
    issue_access_key,
    resolve_access_key,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpAccessKey, McpWorkItem, User


@pytest.fixture
def cli_app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp-cli.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-cli-test",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    _register_mcp_cli(application)
    with application.app_context():
        db.create_all()
        db.session.add(User(id=1, email="cli@example.test", password_hash="x"))
        db.session.add(User(id=2, email="other-cli@example.test", password_hash="x"))
        db.session.commit()
    try:
        yield application, tmp_path
    finally:
        with application.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def test_cli_issues_lists_and_revokes_one_time_bearer(cli_app):
    application, allowed_root = cli_app
    runner = application.test_cli_runner()
    issued = runner.invoke(
        args=[
            "mcp",
            "issue-key",
            "--user-id",
            "1",
            "--label",
            "OpenCode local",
            "--allow-root",
            str(allowed_root),
        ]
    )
    assert issued.exit_code == 0, issued.output
    token_match = re.search(r"mcp_[A-Za-z0-9_-]{43}", issued.output)
    key_match = re.search(
        r"Key id: ([0-9a-f]{8}-[0-9a-f-]{27})",
        issued.output,
    )
    assert token_match is not None
    assert key_match is not None
    token = token_match.group(0)
    key_id = key_match.group(1)

    with application.app_context():
        record = db.session.get(McpAccessKey, key_id)
        assert record is not None
        assert token not in record.token_digest
        assert (
            record.response_default_bytes,
            record.response_max_bytes,
        ) == (8 * 1024 * 1024, 64 * 1024 * 1024)
        assert resolve_access_key(token).owner_user_id == 1

    listed = runner.invoke(args=["mcp", "list-keys", "--user-id", "1"])
    assert listed.exit_code == 0, listed.output
    assert key_id in listed.output
    assert "OpenCode local" in listed.output
    assert token not in listed.output
    listed_fields = next(
        line.split("\t") for line in listed.output.splitlines() if key_id in line
    )
    assert listed_fields[-2:] == [str(8 * 1024 * 1024), str(64 * 1024 * 1024)]

    revoked = runner.invoke(
        args=[
            "mcp",
            "revoke-key",
            "--user-id",
            "1",
            "--key-id",
            key_id,
        ]
    )
    assert revoked.exit_code == 0, revoked.output
    with application.app_context(), pytest.raises(McpAuthError):
        resolve_access_key(token)


def test_cli_issues_custom_response_budgets(cli_app):
    application, _allowed_root = cli_app
    issued = application.test_cli_runner().invoke(
        args=[
            "mcp",
            "issue-key",
            "--user-id",
            "1",
            "--label",
            "Small transport",
            "--response-default-bytes",
            "65536",
            "--response-max-bytes",
            "131072",
        ]
    )
    assert issued.exit_code == 0, issued.output
    key_id = re.search(r"Key id: ([0-9a-f]{8}-[0-9a-f-]{27})", issued.output).group(1)
    with application.app_context():
        record = db.session.get(McpAccessKey, key_id)
        assert record is not None
        assert (record.response_default_bytes, record.response_max_bytes) == (
            65536,
            131072,
        )


def test_cli_configures_transport_and_rejects_invalid_update_atomically(cli_app):
    application, _allowed_root = cli_app
    with application.app_context():
        issued = issue_access_key(
            1,
            "configurable transport",
            ["mcp.access", "mcp.work"],
            [],
        )
        before = db.session.get(McpAccessKey, issued.key_id)
        assert before is not None
        preserved = {
            "owner_user_id": before.owner_user_id,
            "label": before.label,
            "token_digest": before.token_digest,
            "scopes": list(before.scopes),
            "allowed_roots": list(before.allowed_roots),
            "status": before.status,
            "created_at": before.created_at,
            "last_used_at": before.last_used_at,
            "revoked_at": before.revoked_at,
        }

    runner = application.test_cli_runner()
    configured = runner.invoke(
        args=[
            "mcp",
            "configure-transport",
            "--user-id",
            "1",
            "--key-id",
            issued.key_id,
            "--response-default-bytes",
            str(1024 * 1024),
            "--response-max-bytes",
            str(4 * 1024 * 1024),
        ]
    )
    assert configured.exit_code == 0, configured.output
    assert configured.output.splitlines() == [
        f"Key id: {issued.key_id}",
        "Owner user id: 1",
        f"Response default bytes: {1024 * 1024}",
        f"Response max bytes: {4 * 1024 * 1024}",
    ]
    assert issued.raw_token not in configured.output

    invalid_pair = runner.invoke(
        args=[
            "mcp",
            "configure-transport",
            "--user-id",
            "1",
            "--key-id",
            issued.key_id,
            "--response-default-bytes",
            str(4 * 1024 * 1024),
            "--response-max-bytes",
            str(2 * 1024 * 1024),
        ]
    )
    assert invalid_pair.exit_code != 0
    assert "MCP response budgets must be integers" in invalid_pair.output

    out_of_range = runner.invoke(
        args=[
            "mcp",
            "configure-transport",
            "--user-id",
            "1",
            "--key-id",
            issued.key_id,
            "--response-default-bytes",
            "65535",
            "--response-max-bytes",
            "131072",
        ]
    )
    assert out_of_range.exit_code != 0

    with application.app_context():
        after = db.session.get(McpAccessKey, issued.key_id)
        assert after is not None
        assert {
            "owner_user_id": after.owner_user_id,
            "label": after.label,
            "token_digest": after.token_digest,
            "scopes": list(after.scopes),
            "allowed_roots": list(after.allowed_roots),
            "status": after.status,
            "created_at": after.created_at,
            "last_used_at": after.last_used_at,
            "revoked_at": after.revoked_at,
        } == preserved
        assert (after.response_default_bytes, after.response_max_bytes) == (
            1024 * 1024,
            4 * 1024 * 1024,
        )
        assert resolve_access_key(issued.raw_token).response_default_bytes == 1024 * 1024


def test_cli_grants_scopes_without_reissuing_bearer_or_changing_metadata(cli_app):
    application, allowed_root = cli_app
    runner = application.test_cli_runner()
    issued = runner.invoke(
        args=[
            "mcp",
            "issue-key",
            "--user-id",
            "1",
            "--label",
            "Scoped key",
            "--scope",
            "mcp.access",
            "--scope",
            "mcp.doc_processor",
            "--allow-root",
            str(allowed_root),
        ]
    )
    assert issued.exit_code == 0, issued.output
    token = re.search(r"mcp_[A-Za-z0-9_-]{43}", issued.output).group(0)
    key_id = re.search(r"Key id: ([0-9a-f]{8}-[0-9a-f-]{27})", issued.output).group(1)

    with application.app_context():
        before = db.session.get(McpAccessKey, key_id)
        assert before is not None
        preserved = {
            "owner_user_id": before.owner_user_id,
            "label": before.label,
            "token_digest": before.token_digest,
            "allowed_roots": before.allowed_roots,
            "status": before.status,
            "created_at": before.created_at,
            "last_used_at": before.last_used_at,
            "revoked_at": before.revoked_at,
        }

    granted = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "1",
            "--key-id",
            key_id,
            "--scope",
            "mcp.news_pipeline",
        ]
    )
    assert granted.exit_code == 0, granted.output
    assert granted.output.splitlines() == [
        f"Key id: {key_id}",
        "Owner user id: 1",
        "Scopes: mcp.access, mcp.doc_processor, mcp.news_pipeline",
    ]
    assert token not in granted.output
    with application.app_context():
        after = db.session.get(McpAccessKey, key_id)
        assert after is not None
        assert {
            "owner_user_id": after.owner_user_id,
            "label": after.label,
            "token_digest": after.token_digest,
            "allowed_roots": after.allowed_roots,
            "status": after.status,
            "created_at": after.created_at,
            "last_used_at": after.last_used_at,
            "revoked_at": after.revoked_at,
        } == preserved
        assert after.scopes == [
            "mcp.access",
            "mcp.doc_processor",
            "mcp.news_pipeline",
        ]
        assert resolve_access_key(token).scopes == frozenset(after.scopes)

    repeated = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "1",
            "--key-id",
            key_id,
            "--scope",
            "mcp.news_pipeline",
        ]
    )
    assert repeated.exit_code == 0, repeated.output
    with application.app_context():
        assert db.session.get(McpAccessKey, key_id).scopes == [
            "mcp.access",
            "mcp.doc_processor",
            "mcp.news_pipeline",
        ]


def test_cli_grant_scopes_rejects_missing_owner_revoked_and_malformed_requests(cli_app):
    application, _allowed_root = cli_app
    runner = application.test_cli_runner()
    with application.app_context():
        owner_key = issue_access_key(
            1, "owner", ["mcp.access", "mcp.doc_processor"], []
        )
        other_key = issue_access_key(2, "other", ["mcp.access"], [])

    wrong_owner = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "2",
            "--key-id",
            owner_key.key_id,
            "--scope",
            "mcp.news_pipeline",
        ]
    )
    assert wrong_owner.exit_code != 0
    assert "The MCP access key was not found." in wrong_owner.output

    malformed = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "1",
            "--key-id",
            owner_key.key_id,
            "--scope",
            "MCP.NEWS",
        ]
    )
    assert malformed.exit_code != 0
    assert "Access-key scopes are malformed." in malformed.output

    no_scope = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "2",
            "--key-id",
            other_key.key_id,
        ]
    )
    assert no_scope.exit_code != 0
    assert "Missing option '--scope'." in no_scope.output

    revoked = runner.invoke(
        args=[
            "mcp",
            "revoke-key",
            "--user-id",
            "1",
            "--key-id",
            owner_key.key_id,
        ]
    )
    assert revoked.exit_code == 0, revoked.output
    grant_revoked = runner.invoke(
        args=[
            "mcp",
            "grant-scopes",
            "--user-id",
            "1",
            "--key-id",
            owner_key.key_id,
            "--scope",
            "mcp.news_pipeline",
        ]
    )
    assert grant_revoked.exit_code != 0
    assert "The MCP access key is revoked." in grant_revoked.output

    with application.app_context():
        assert db.session.get(McpAccessKey, other_key.key_id).scopes == ["mcp.access"]


def test_grant_scopes_rejects_stale_json_compare_and_set(cli_app, monkeypatch):
    application, _allowed_root = cli_app
    with application.app_context():
        issued = issue_access_key(1, "stale", ["mcp.access"], [])
        record = db.session.get(McpAccessKey, issued.key_id)
        assert record is not None
        session = db.session()
        original_execute = session.execute
        state = {"interleaved": False}

        def execute(statement, *args, **kwargs):
            if (
                not state["interleaved"]
                and getattr(getattr(statement, "table", None), "name", None)
                == "mcp_access_key"
            ):
                state["interleaved"] = True
                session.rollback()
                with db.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE mcp_access_key SET scopes = :scopes "
                            "WHERE key_id = :key_id"
                        ),
                        {
                            "key_id": issued.key_id,
                            "scopes": json.dumps(["mcp.access", "mcp.translator"]),
                        },
                    )
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", execute)
        with pytest.raises(McpAuthError) as error:
            grant_access_key_scopes(
                issued.key_id,
                owner_user_id=1,
                scopes=["mcp.news_pipeline"],
            )
        assert error.value.code == "key_changed"
        assert error.value.status_code == 409
        assert error.value.detail == (
            "The MCP access key changed. Read its current state and retry."
        )
        session.rollback()
        current = db.session.get(McpAccessKey, issued.key_id)
        assert current is not None
        assert current.scopes == ["mcp.access", "mcp.translator"]


def test_cli_terminal_redaction_defaults_to_dry_run_and_apply_mutates(cli_app):
    application, _allowed_root = cli_app
    with application.app_context():
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "terminal-document",
            {"prompt": "private execution request"},
            mode="passive",
        )
        work = get_or_create_work(execution.id, {"prompt": "private work request"})
        execution.status = "completed"
        db.session.commit()
        raw_request_before = db.session.execute(
            text("SELECT request_json FROM mcp_work_item WHERE id = :id"),
            {"id": work.work_item_id},
        ).scalar_one()

    runner = application.test_cli_runner()
    dry_run = runner.invoke(
        args=["mcp", "redact-terminal-work", "--batch-size", "1"]
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert "Mode: dry-run" in dry_run.output
    assert "Rows eligible: 1" in dry_run.output
    assert "Rows redacted: 0" in dry_run.output
    assert "private work request" not in dry_run.output
    with application.app_context():
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.request_pruned_at is None
        assert db.session.execute(
            text("SELECT request_json FROM mcp_work_item WHERE id = :id"),
            {"id": work.work_item_id},
        ).scalar_one() == raw_request_before

    applied = runner.invoke(
        args=["mcp", "redact-terminal-work", "--apply", "--batch-size", "1"]
    )
    assert applied.exit_code == 0, applied.output
    assert "Mode: applied" in applied.output
    assert "Rows eligible: 1" in applied.output
    assert "Rows redacted: 1" in applied.output
    with application.app_context():
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.request_json == MCP_REQUEST_TOMBSTONE
        assert item.request_pruned_at is not None
