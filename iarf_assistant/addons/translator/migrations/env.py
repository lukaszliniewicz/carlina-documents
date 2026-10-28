import sys
from pathlib import Path
from logging.config import fileConfig
import importlib

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# The main Flask app already configures logging, so this is not needed.
# if config.config_file_name is not None:
#     fileConfig(config.config_file_name)

# Add the project root to the Python path.
# This is necessary for Alembic to find the app's models.
# The path is migrations/ -> addon/ -> addons/ -> iarf_assistant/ -> project_root
project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Import your app's base model (db.Model from extensions.py)
# and any other models needed. Alembic needs this to auto-generate migrations.
from iarf_assistant.extensions import db
from carlina.migration_rendering import render_storage_type

# Dynamically import the models from the addon this migration belongs to.
addon_id = "translator"
try:
    importlib.import_module(f"iarf_assistant.addons.{addon_id}.models")
except ImportError:
    # It's fine if an addon has no models.py file initially.
    pass

# The target_metadata is the SQLAlchemy MetaData object from your application's models.
target_metadata = db.metadata

def include_object(object, name, type_, reflected, compare_to):
    """
    Function to filter which objects are included in the autogeneration.
    For an addon migration, we only want objects from that addon's models.py.
    """
    if type_ == 'table':
        # Find the model class associated with this table from the registry
        for mapper in db.Model.registry.mappers:
            if mapper.local_table is object:
                module = mapper.class_.__module__
                return module and module.startswith(f'iarf_assistant.addons.translator.')
        return False
    elif hasattr(object, 'table') and object.table is not None:
        # For indexes, constraints, etc., check if their parent table is included.
        return include_object(object.table, object.table.name, 'table', reflected, compare_to)
    return False

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


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
    # Re-use the app's configured engine
    connectable = db.engine

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True, # Added for SQLite support
            include_object=include_object,
            render_item=render_storage_type,
            # Explicitly set the version table to ensure isolation
            version_table=config.get_main_option('version_table')
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
