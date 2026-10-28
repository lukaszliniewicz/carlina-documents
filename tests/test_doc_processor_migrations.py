from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_ROOT = (
    PROJECT_ROOT
    / "iarf_assistant"
    / "addons"
    / "doc_processor"
    / "migrations"
)


def _migration_config(database_path: Path) -> Config:
    config = Config(str(MIGRATIONS_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(MIGRATIONS_ROOT))
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite:///{database_path.as_posix()}",
    )
    return config


def test_legacy_metadata_revision_upgrades_through_compatibility_bridge(tmp_path: Path):
    database_path = tmp_path / "doc-processor-migrations.db"
    config = _migration_config(database_path)
    command.upgrade(config, "003_doc_metadata_ownership")

    with sqlite3.connect(database_path) as connection:
        current_revision = connection.execute(
            "SELECT version_num FROM alembic_version_doc_processor"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO processed_document
                (id, user_id, filename, file_path, status, settings)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-doc",
                1,
                "legacy.txt",
                "/uploads/legacy.txt",
                "completed",
                json.dumps(
                    {
                        "original_output_backup": "sensitive plaintext",
                        "keep": "yes",
                    }
                ),
            ),
        )
        connection.commit()
    assert current_revision == "003_doc_metadata_ownership"

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        head_revision = connection.execute(
            "SELECT version_num FROM alembic_version_doc_processor"
        ).fetchone()[0]
        settings = json.loads(
            connection.execute(
                "SELECT settings FROM processed_document WHERE id = ?",
                ("legacy-doc",),
            ).fetchone()[0]
        )
        processed_document_indexes = {
            row[1]
            for row in connection.execute(
                "PRAGMA index_list('processed_document')"
            )
        }

    assert head_revision == "006_epub_editions"
    assert settings == {"keep": "yes"}
    assert "ix_processed_document_user_id" in processed_document_indexes

    # Head retry is inert; reverting the additive ledger schema preserves the
    # pre-existing document. A later upgrade recreates only the new table.
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info('doc_processor_issue')")}
        assert {"payload", "owner_user_id", "source_revision", "version"} <= columns
        foreign_keys = list(connection.execute("PRAGMA foreign_key_list('doc_processor_issue')"))
        assert {row[2] for row in foreign_keys} == {"processed_document", "user"}
        assert all(row[6] == "CASCADE" for row in foreign_keys)
    command.downgrade(config, "004_doc_processor_safety_indexes")
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT id FROM processed_document").fetchall() == [("legacy-doc",)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='doc_processor_issue'").fetchone() is None
    command.upgrade(config, "head")
