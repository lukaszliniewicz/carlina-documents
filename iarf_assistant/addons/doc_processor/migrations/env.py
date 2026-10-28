from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from carlina.migration_rendering import render_storage_type

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

project_root = Path(__file__).resolve().parents[4]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import iarf_assistant.addons.doc_processor.models
import iarf_assistant.models  # noqa: F401
from iarf_assistant.extensions import db

target_metadata = db.metadata
ADDON_TABLES = {
    "processed_document",
    "document_metadata",
    "document_project",
    "project_document",
    "project_run",
    "project_run_document",
    "doc_processor_issue",
}
VERSION_TABLE = "alembic_version_doc_processor"


def include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in ADDON_TABLES

    table = getattr(object_, "table", None)
    if table is not None:
        return table.name in ADDON_TABLES

    return True


def run_migrations_offline():
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        version_table=VERSION_TABLE,
        render_as_batch=True,
        compare_type=True,
        render_item=render_storage_type,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        with connectable.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                include_object=include_object,
                version_table=VERSION_TABLE,
                render_as_batch=True,
                compare_type=True,
                render_item=render_storage_type,
            )

            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
