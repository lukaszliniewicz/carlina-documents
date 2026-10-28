"""Execute reviewed add-on revisions without importing package-owned env.py."""

from __future__ import annotations

from typing import Any

from alembic import context

# Historical reviewed revisions accessed EncryptedType through a legacy package
# attribute after their old env.py imported add-on models. Preload only this
# stable core migration type; never import an add-on's package-owned environment
# or models here.
import iarf_assistant.database.types  # noqa: F401

config = context.config


def _context_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "target_metadata": None,
        "render_as_batch": True,
        "version_table": config.get_main_option("version_table"),
        "version_num_length": int(config.get_main_option("version_num_length") or "32"),
    }
    version_schema = config.get_main_option("version_table_schema")
    if version_schema:
        options["version_table_schema"] = version_schema
    return options


def run_migrations_offline() -> None:
    raise RuntimeError(
        "Offline SQL generation is not supported for add-on migrations. "
        "Run migrations through Carlina's online lifecycle service."
    )


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "Carlina's add-on migration runtime requires a platform-owned "
            "database connection."
        )
    context.configure(connection=connection, **_context_options())
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
