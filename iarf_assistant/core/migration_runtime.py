from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from carlina.product_identity import product_environment_value

_TRUE_VALUES = {"1", "true", "yes", "on"}
_MIGRATION_COMMANDS = {
    "--help",
    "-h",
    "db",
    "init-app",
    "reset-db",
}


def migrations_directory() -> Path:
    """Return the immutable Alembic history shipped inside the package."""

    return Path(__file__).resolve().parents[1] / "migrations"


def migration_cli_requested(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Returns whether this process needs Flask-Migrate's CLI commands."""
    environment = environ if environ is not None else os.environ
    explicit = product_environment_value(
        "REGISTER_MIGRATION_CLI",
        environment=environment,
    )
    if explicit is not None:
        return explicit.strip().lower() in _TRUE_VALUES

    arguments = argv if argv is not None else sys.argv
    return any(
        str(argument).strip().lower() in _MIGRATION_COMMANDS
        for argument in arguments[1:]
    )


def configure_migration_cli(
    app: Any,
    db: Any,
    *,
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Registers Alembic commands only in migration-management processes."""
    if not migration_cli_requested(argv=argv, environ=environ):
        return False

    from flask_migrate import Migrate

    Migrate(
        app,
        db,
        directory=str(migrations_directory()),
        render_as_batch=True,
    )
    return True


__all__ = [
    "configure_migration_cli",
    "migration_cli_requested",
    "migrations_directory",
]
