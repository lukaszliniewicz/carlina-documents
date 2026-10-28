from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "iarf_assistant"
    / "migrations"
    / "versions"
    / "20260926_mcp_transport.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "mcp_transport_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke(migration, direction: str, connection: sa.Connection) -> None:
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


class _InterruptAfterFirstDDL:
    def __init__(self, operations, connection: sa.Connection, operation: str) -> None:
        self._operations = operations
        self._connection = connection
        self._operation = operation
        self._interrupted = False

    def __getattr__(self, name):
        return getattr(self._operations, name)

    def _run(self, name: str, *args, **kwargs):
        result = getattr(self._operations, name)(*args, **kwargs)
        if name == self._operation and not self._interrupted:
            self._interrupted = True
            self._connection.commit()
            raise RuntimeError("simulated migration interruption")
        return result

    def add_column(self, *args, **kwargs):
        return self._run("add_column", *args, **kwargs)

    def drop_column(self, *args, **kwargs):
        return self._run("drop_column", *args, **kwargs)


def _interrupt_after_first_ddl(
    migration,
    direction: str,
    connection: sa.Connection,
    operation: str,
) -> None:
    migration.op = _InterruptAfterFirstDDL(
        Operations(MigrationContext.configure(connection)),
        connection,
        operation,
    )
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        getattr(migration, direction)()


def _assert_original_key_and_receipt(
    connection: sa.Connection,
    original: tuple,
) -> None:
    row = connection.exec_driver_sql(
        """
        SELECT owner_user_id, label, token_digest, scopes, allowed_roots,
               status, created_at, last_used_at, revoked_at
        FROM mcp_access_key WHERE key_id = 'legacy-key'
        """
    ).one()
    assert tuple(row) == original
    assert connection.exec_driver_sql(
        "SELECT COUNT(*) FROM mcp_work_claim_receipt WHERE key_id = 'legacy-key'"
    ).scalar_one() == 1
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_transport_migration_preserves_existing_key_bytes_and_foreign_keys(
    tmp_path: Path,
):
    database_path = tmp_path / "mcp-transport-migration.db"
    engine = sa.create_engine(f"sqlite:///{database_path}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    migration = _load_migration()
    assert migration.revision == "20260926_mcp_transport"
    assert migration.down_revision == "20260920_news_mcp"

    original = (
        1,
        "enc:v2:synthetic-label-ciphertext",
        "lh1:synthetic-keyed-digest",
        '["mcp.access","mcp.work"]',
        "enc:v2:synthetic-roots-ciphertext",
        "active",
        "2026-09-25 12:00:00",
        None,
        None,
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql(
            """
            CREATE TABLE mcp_access_key (
                key_id VARCHAR(36) PRIMARY KEY,
                owner_user_id INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
                label TEXT NOT NULL,
                token_digest TEXT NOT NULL,
                scopes TEXT NOT NULL,
                allowed_roots TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE mcp_work_claim_receipt (
                id VARCHAR(36) PRIMARY KEY,
                key_id VARCHAR(36) NOT NULL
                    REFERENCES mcp_access_key(key_id) ON DELETE CASCADE
            )
            """
        )
        connection.exec_driver_sql("INSERT INTO user (id) VALUES (1)")
        connection.exec_driver_sql(
            """
            INSERT INTO mcp_access_key (
                key_id, owner_user_id, label, token_digest, scopes,
                allowed_roots, status, created_at, last_used_at, revoked_at
            ) VALUES ('legacy-key', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            original,
        )
        connection.exec_driver_sql(
            "INSERT INTO mcp_work_claim_receipt (id, key_id) VALUES ('receipt-1', 'legacy-key')"
        )
        connection.commit()
        before_rootpage = connection.exec_driver_sql(
            "SELECT rootpage FROM sqlite_master "
            "WHERE type = 'table' AND name = 'mcp_access_key'"
        ).scalar_one()
        connection.commit()

        _interrupt_after_first_ddl(
            migration,
            "upgrade",
            connection,
            "add_column",
        )
        partial_columns = {
            row["name"] for row in sa.inspect(connection).get_columns("mcp_access_key")
        }
        assert "response_default_bytes" in partial_columns
        assert "response_max_bytes" not in partial_columns
        _assert_original_key_and_receipt(connection, original)
        connection.commit()

        _invoke(migration, "upgrade", connection)
        connection.commit()
        _invoke(migration, "upgrade", connection)
        connection.commit()

        after_rootpage = connection.exec_driver_sql(
            "SELECT rootpage FROM sqlite_master "
            "WHERE type = 'table' AND name = 'mcp_access_key'"
        ).scalar_one()
        assert after_rootpage == before_rootpage
        migrated = connection.exec_driver_sql(
            """
            SELECT owner_user_id, label, token_digest, scopes, allowed_roots,
                   status, created_at, last_used_at, revoked_at,
                   response_default_bytes, response_max_bytes
            FROM mcp_access_key WHERE key_id = 'legacy-key'
            """
        ).one()
        assert tuple(migrated[:9]) == original
        assert migrated[9:] == (8 * 1024 * 1024, 64 * 1024 * 1024)
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM mcp_work_claim_receipt WHERE key_id = 'legacy-key'"
        ).scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()

        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "UPDATE mcp_access_key SET response_default_bytes = 65535 "
                "WHERE key_id = 'legacy-key'"
            )
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "UPDATE mcp_access_key SET response_max_bytes = 67108865 "
                "WHERE key_id = 'legacy-key'"
            )
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "UPDATE mcp_access_key SET response_max_bytes = 4194304 "
                "WHERE key_id = 'legacy-key'"
            )

        connection.commit()
        _interrupt_after_first_ddl(
            migration,
            "downgrade",
            connection,
            "drop_column",
        )
        remaining_columns = {
            row["name"] for row in sa.inspect(connection).get_columns("mcp_access_key")
        }
        assert "response_default_bytes" in remaining_columns
        assert "response_max_bytes" not in remaining_columns
        _assert_original_key_and_receipt(connection, original)
        connection.commit()

        _invoke(migration, "downgrade", connection)
        connection.commit()
        _invoke(migration, "downgrade", connection)
        connection.commit()
        remaining_columns = {
            row["name"] for row in sa.inspect(connection).get_columns("mcp_access_key")
        }
        assert "response_default_bytes" not in remaining_columns
        assert "response_max_bytes" not in remaining_columns
        restored = connection.exec_driver_sql(
            """
            SELECT owner_user_id, label, token_digest, scopes, allowed_roots,
                   status, created_at, last_used_at, revoked_at
            FROM mcp_access_key WHERE key_id = 'legacy-key'
            """
        ).one()
        assert tuple(restored) == original
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM mcp_work_claim_receipt WHERE key_id = 'legacy-key'"
        ).scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()

        connection.exec_driver_sql(
            "ALTER TABLE mcp_access_key "
            "ADD COLUMN response_default_bytes INTEGER NOT NULL DEFAULT 1"
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="incompatible response_default_bytes"):
            _invoke(migration, "upgrade", connection)
        incompatible_columns = {
            row["name"] for row in sa.inspect(connection).get_columns("mcp_access_key")
        }
        assert "response_max_bytes" not in incompatible_columns

    engine.dispose()
