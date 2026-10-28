import sys
from pathlib import Path
from logging.config import fileConfig
import os
import importlib

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Add the project root to the Python path.
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import base model and all models for autogenerate
from iarf_assistant.extensions import db
# This will import all models from the main application
from iarf_assistant.models import *
from carlina.migration_rendering import render_storage_type

# Although we don't explicitly import addon models, the app context might.
# We must filter metadata to only include main app tables for this migration.
target_metadata = db.metadata

def include_object(object, name, type_, reflected, compare_to):
    """
    Function to filter which objects are included in the autogeneration.
    For the main app migration, we only want objects from `iarf_assistant.models`.
    """
    if type_ == 'table':
        if hasattr(object, 'c') and hasattr(object.c, '_sa_class_manager'):
            module = object.c._sa_class_manager.class_.__module__
            return module == 'iarf_assistant.models'
        return False
    elif hasattr(object, 'table') and object.table is not None:
         # For indexes, constraints, etc., check if their parent table is included.
        return include_object(object.table, object.table.name, 'table', reflected, compare_to)
    return False

def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True, # Added for SQLite support
        include_object=include_object,
        render_item=render_storage_type
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # When running with flask-migrate, an application context is available,
    # so db.engine is already configured.
    connectable = db.engine

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True, # Added for SQLite support
            include_object=include_object,
            render_item=render_storage_type
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
