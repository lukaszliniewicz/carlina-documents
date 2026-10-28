import os
import shutil
from pathlib import Path

import click

from carlina.core.managed_target_recovery import (
    resolve_managed_target_recovery_context,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ApiKey,
    CredentialDelegation,
    DataContext,
    LLMModel,
    LLMProvider,
    PluginCredential,
    Setting,
    User,
)


def _create_first_admin(email: str, password: str) -> bool:
    """Serialize the first-user decision; never replace an existing account."""
    from sqlalchemy import text

    # Seeding has committed. End its final read transaction before taking the
    # SQLite writer reservation, including on two independent setup connections.
    db.session.rollback()
    try:
        if db.engine.dialect.name != "sqlite":
            raise RuntimeError("Unsupported initialization database.")
        db.session.execute(text("BEGIN IMMEDIATE"))
        if db.session.query(User.id).first() is not None:
            db.session.rollback()
            return False
        user = User(email=email, is_admin=True)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise click.ClickException("Administrator creation failed.") from None


def _register_mcp_cli(app):
    """Register local operator commands for digest-only MCP access keys."""

    from iarf_assistant.core.mcp_jobs import (
        MCP_DEFAULT_RESPONSE_BYTES,
        MCP_MAX_RESPONSE_BYTES,
        MCP_MIN_RESPONSE_BYTES,
    )

    @app.cli.group("mcp")
    def mcp_group():
        """Manage the loopback MCP product gateway."""

    @mcp_group.command("issue-key")
    @click.option("--user-id", type=click.IntRange(min=1), required=True)
    @click.option("--label", required=True, help="Operator-facing key label.")
    @click.option(
        "--scope",
        "scopes",
        multiple=True,
        help="Repeat to narrow this application's default product and work scopes.",
    )
    @click.option(
        "--allow-root",
        "allowed_roots",
        multiple=True,
        type=click.Path(exists=True, file_okay=False, resolve_path=True),
        help="Repeat for each local source-import directory this key may read.",
    )
    @click.option(
        "--response-default-bytes",
        type=click.IntRange(
            min=MCP_MIN_RESPONSE_BYTES,
            max=MCP_MAX_RESPONSE_BYTES,
        ),
        default=MCP_DEFAULT_RESPONSE_BYTES,
        show_default=True,
        help="Default response budget in bytes for this MCP key.",
    )
    @click.option(
        "--response-max-bytes",
        type=click.IntRange(
            min=MCP_MIN_RESPONSE_BYTES,
            max=MCP_MAX_RESPONSE_BYTES,
        ),
        default=MCP_MAX_RESPONSE_BYTES,
        show_default=True,
        help="Largest response budget in bytes permitted for this MCP key.",
    )
    def issue_mcp_key(
        user_id,
        label,
        scopes,
        allowed_roots,
        response_default_bytes,
        response_max_bytes,
    ):
        """Issue one owner-scoped bearer value and display it exactly once."""

        from iarf_assistant.core.mcp_gateway import mcp_default_scopes, mcp_effective_scopes
        from iarf_assistant.core.mcp_jobs import McpError, issue_access_key

        if db.session.get(User, user_id) is None:
            raise click.ClickException("The requested key owner does not exist.")
        selected_scopes = list(scopes or mcp_default_scopes(app))
        if set(selected_scopes) != mcp_effective_scopes(app, selected_scopes):
            raise click.ClickException(
                "A requested MCP scope is unavailable in this application profile."
            )
        try:
            issued = issue_access_key(
                user_id,
                label,
                selected_scopes,
                list(allowed_roots),
                response_default_bytes=response_default_bytes,
                response_max_bytes=response_max_bytes,
            )
        except McpError as exc:
            raise click.ClickException(exc.detail) from exc
        click.echo(f"Key id: {issued.key_id}")
        click.echo(f"Owner user id: {issued.owner_user_id}")
        click.echo(f"Scopes: {', '.join(sorted(selected_scopes))}")
        click.echo("Bearer token (shown once):")
        click.echo(issued.raw_token)

    @mcp_group.command("list-keys")
    @click.option("--user-id", type=click.IntRange(min=1), required=True)
    def list_mcp_keys(user_id):
        """List safe metadata for one owner's MCP keys."""

        from iarf_assistant.models import McpAccessKey

        records = (
            McpAccessKey.query.filter_by(owner_user_id=user_id)
            .order_by(McpAccessKey.created_at.desc(), McpAccessKey.key_id.asc())
            .all()
        )
        if not records:
            click.echo("No MCP access keys found.")
            return
        for record in records:
            scopes = ",".join(sorted(str(value) for value in (record.scopes or [])))
            click.echo(
                "\t".join(
                    (
                        str(record.key_id),
                        str(record.status),
                        str(record.label),
                        scopes,
                        record.created_at.isoformat() if record.created_at else "",
                        record.last_used_at.isoformat() if record.last_used_at else "never",
                        str(record.response_default_bytes),
                        str(record.response_max_bytes),
                    )
                )
            )

    @mcp_group.command("revoke-key")
    @click.option("--user-id", type=click.IntRange(min=1), required=True)
    @click.option("--key-id", required=True)
    def revoke_mcp_key(user_id, key_id):
        """Revoke one exact owner/key pair."""

        from iarf_assistant.core.mcp_jobs import McpError, revoke_access_key

        try:
            record = revoke_access_key(key_id, owner_user_id=user_id)
            db.session.commit()
        except McpError as exc:
            db.session.rollback()
            raise click.ClickException(exc.detail) from exc
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Revoked MCP key {record.key_id} for user {user_id}.")

    @mcp_group.command("grant-scopes")
    @click.option("--user-id", type=click.IntRange(min=1), required=True)
    @click.option("--key-id", required=True)
    @click.option(
        "--scope",
        "scopes",
        multiple=True,
        required=True,
        help="Repeat for each scope to add to this existing key.",
    )
    def grant_mcp_key_scopes(user_id, key_id, scopes):
        """Add scopes to one exact owner/key pair."""

        from iarf_assistant.core.mcp_jobs import McpError, grant_access_key_scopes

        try:
            record = grant_access_key_scopes(
                key_id,
                owner_user_id=user_id,
                scopes=scopes,
            )
            db.session.commit()
        except McpError as exc:
            db.session.rollback()
            raise click.ClickException(exc.detail) from exc
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Key id: {record.key_id}")
        click.echo(f"Owner user id: {record.owner_user_id}")
        click.echo(
            f"Scopes: {', '.join(sorted(str(value) for value in (record.scopes or [])))}"
        )

    @mcp_group.command("configure-transport")
    @click.option("--user-id", type=click.IntRange(min=1), required=True)
    @click.option("--key-id", required=True)
    @click.option(
        "--response-default-bytes",
        type=click.IntRange(
            min=MCP_MIN_RESPONSE_BYTES,
            max=MCP_MAX_RESPONSE_BYTES,
        ),
        required=True,
        help="Default response budget in bytes for this MCP key.",
    )
    @click.option(
        "--response-max-bytes",
        type=click.IntRange(
            min=MCP_MIN_RESPONSE_BYTES,
            max=MCP_MAX_RESPONSE_BYTES,
        ),
        required=True,
        help="Largest response budget in bytes permitted for this MCP key.",
    )
    def configure_mcp_transport(
        user_id,
        key_id,
        response_default_bytes,
        response_max_bytes,
    ):
        """Set response budgets for one exact owner/key pair without rotating its bearer."""

        from iarf_assistant.core.mcp_jobs import (
            McpError,
            configure_access_key_transport,
        )

        try:
            record = configure_access_key_transport(
                key_id,
                owner_user_id=user_id,
                response_default_bytes=response_default_bytes,
                response_max_bytes=response_max_bytes,
            )
            db.session.commit()
        except McpError as exc:
            db.session.rollback()
            raise click.ClickException(exc.detail) from exc
        except Exception:
            db.session.rollback()
            raise
        click.echo(f"Key id: {record.key_id}")
        click.echo(f"Owner user id: {record.owner_user_id}")
        click.echo(f"Response default bytes: {record.response_default_bytes}")
        click.echo(f"Response max bytes: {record.response_max_bytes}")

    @mcp_group.command("redact-terminal-work")
    @click.option("--apply", is_flag=True, help="Commit terminal request redaction.")
    @click.option("--user-id", type=click.IntRange(min=1), default=None)
    @click.option(
        "--batch-size",
        type=click.IntRange(min=1, max=1000),
        default=100,
        show_default=True,
    )
    def redact_terminal_work_command(apply, user_id, batch_size):
        """Preview or apply bounded redaction of terminal MCP work requests."""

        from iarf_assistant.core.mcp_jobs import McpError, redact_terminal_work

        try:
            report = redact_terminal_work(
                owner_user_id=user_id,
                batch_size=batch_size,
                dry_run=not apply,
            )
        except McpError as exc:
            raise click.ClickException(exc.detail) from exc
        mode = "applied" if apply else "dry-run"
        click.echo(f"Mode: {mode}")
        click.echo(f"Rows eligible: {report['rows_eligible']}")
        click.echo(f"Rows redacted: {report['rows_redacted']}")
        click.echo(
            "Encrypted request bytes before redaction: "
            f"{report['encrypted_request_bytes_before_redaction']}"
        )
        click.echo(f"Batches: {report['batches']}")


def _exception_detail(error: BaseException) -> str:
    """Return a useful operator-facing detail even for message-less errors."""

    detail = str(error).strip()
    return detail or type(error).__name__


def _reconcile_builtin_addons_after_core_migration(app):
    addon_manager = getattr(app, "addon_manager", None)
    if addon_manager is None:
        return None
    managed_recovery = resolve_managed_target_recovery_context()
    with addon_manager.lifecycle_maintenance_authority():
        if managed_recovery is not None:
            app.logger.info(
                "Built-in migrations are reusing managed recovery for "
                "transaction %s (%s).",
                managed_recovery.transaction_id,
                managed_recovery.selected_coverage.value,
            )
            return addon_manager.reconcile_builtin_addons(
                managed_recovery=managed_recovery
            )
        return addon_manager.reconcile_builtin_addons()


def register_cli(app, *, assistant_services: bool = True):
    _register_mcp_cli(app)

    @app.cli.command("init-app")
    @click.option('--admin-email', default=None, help="Email address for the first admin user.")
    @click.option('--admin-password', default=None, help="Password for the first admin user.")
    @click.option('--no-admin-prompt', is_flag=True, help="Do not prompt when no users exist.")
    @click.option('--admin-stdin', is_flag=True, help="Read first-administrator credentials from a private JSON pipe.")
    def init_app_command(admin_email, admin_password, no_admin_prompt, admin_stdin=False):
        """Initializes DB, runs migrations, seeds data, and creates the first admin user."""
        from flask_migrate import upgrade as migrate_upgrade

        from iarf_assistant.core.migration_runtime import migrations_directory

        if admin_stdin:
            from carlina.core.initial_admin import InitialAdminInputError, read_initial_admin

            if admin_email is not None or admin_password is not None or no_admin_prompt:
                raise click.ClickException("--admin-stdin cannot be combined with other administrator options.")
            try:
                credentials = read_initial_admin(click.get_binary_stream("stdin"))
            except InitialAdminInputError:
                raise click.ClickException("Initial administrator input is invalid.") from None
            admin_email, admin_password = credentials.email, credentials.password

        click.echo("Initializing application...")
        with app.app_context():
            migrations_dir = migrations_directory()
            if not (migrations_dir / "alembic.ini").is_file() or not (
                migrations_dir / "versions"
            ).is_dir():
                raise click.ClickException(
                    "The packaged Carlina migration history is missing. "
                    "Reinstall a verified distribution before initializing data."
                )

            click.echo("Applying database migrations...")
            try:
                migrate_upgrade(directory=str(migrations_dir))
                click.echo("Database is up to date.")
            except Exception as e:
                app.logger.exception("Database migration failed.")
                raise click.ClickException(
                    "Error applying database migrations: "
                    f"{_exception_detail(e)}"
                ) from e

            if getattr(app, "addon_manager", None) is not None:
                click.echo("Reconciling active built-in add-ons...")
                try:
                    reconciliation = _reconcile_builtin_addons_after_core_migration(app)
                except Exception as e:
                    raise click.ClickException(
                        f"Error reconciling built-in add-ons: {e}"
                    ) from e
                reconciliation = reconciliation or {}
                reconciled = reconciliation.get("reconciled", [])
                deferred = reconciliation.get("deferred_inactive", [])
                prepared = reconciliation.get("prepared_environments", [])
                indexed = reconciliation.get("prepared_import_indexes", [])
                click.echo(
                    f"Built-in add-ons are up to date "
                    f"({len(reconciled)} reconciled, "
                    f"{len(prepared)} dependency environments prepared, "
                    f"{len(indexed)} import indexes prepared, "
                    f"{len(deferred)} inactive upgrades deferred)."
                )

            click.echo("Seeding default data...")
            try:
                click.echo("Checking LLM providers...")
                default_providers = [
                    {'name': 'openai', 'provider_kind': 'native', 'discovery_type': 'automatic', 'supports_custom_models': False, 'requires_api_base': False},
                    {'name': 'gemini', 'provider_kind': 'native', 'discovery_type': 'automatic', 'supports_custom_models': True, 'requires_api_base': False},
                    {'name': 'anthropic', 'provider_kind': 'native', 'discovery_type': 'automatic', 'supports_custom_models': False, 'requires_api_base': False},
                    {'name': 'vertex_ai', 'provider_kind': 'native', 'discovery_type': 'manual', 'supports_custom_models': True, 'requires_api_base': True},
                    {'name': 'lmstudio', 'provider_kind': 'openai_compatible', 'discovery_type': 'automatic', 'supports_custom_models': True, 'requires_api_base': True},
                    {'name': 'openrouter', 'provider_kind': 'native', 'discovery_type': 'manual', 'supports_custom_models': True, 'requires_api_base': False},
                    {'name': 'groq', 'provider_kind': 'native', 'discovery_type': 'automatic', 'supports_custom_models': False, 'requires_api_base': False},
                    {'name': 'vllm', 'provider_kind': 'openai_compatible', 'discovery_type': 'automatic', 'supports_custom_models': True, 'requires_api_base': True},
                    {'name': 'azure', 'provider_kind': 'native', 'discovery_type': 'manual', 'supports_custom_models': True, 'requires_api_base': True},
                    {'name': 'xiaomi_mimo', 'provider_kind': 'native', 'discovery_type': 'manual', 'supports_custom_models': True, 'requires_api_base': False}
                ]
                for provider_data in default_providers:
                    existing = db.session.query(LLMProvider).filter_by(name=provider_data['name']).first()
                    if not existing:
                        provider = LLMProvider(**provider_data)
                        db.session.add(provider)
                        click.echo(f"Added missing provider: {provider_data['name']}")
                    else:
                        # Update existing provider if capabilities changed in the codebase
                        updated = False
                        for key, val in provider_data.items():
                            if getattr(existing, key) != val:
                                setattr(existing, key, val)
                                updated = True
                        if updated:
                            click.echo(f"Updated provider config for: {provider_data['name']}")
                db.session.commit()
            except Exception as e:
                raise click.ClickException(f"Error seeding data: {e}")

            # After seeding, check if a first user needs to be created
            if User.query.first() is None:
                click.echo("-" * 40)
                click.echo("No users found in the database.")
                if admin_email or admin_password:
                    if not admin_email or not admin_password:
                        click.echo("Error: --admin-email and --admin-password must be provided together.")
                        return
                    if _create_first_admin(admin_email, admin_password):
                        click.echo(f"Admin user '{admin_email}' created successfully.")
                    else:
                        click.echo("An administrator account already exists.")
                elif no_admin_prompt:
                    click.echo("Skipping admin user creation. Run 'flask create-user EMAIL PASSWORD --admin' later.")
                elif click.confirm("Do you want to create an admin user now?"):
                    email = click.prompt("Enter admin email", type=str)
                    password = click.prompt("Enter admin password", hide_input=True, confirmation_prompt=True)
                    if _create_first_admin(email, password):
                        click.echo(f"Admin user '{email}' created successfully.")
                    else:
                        click.echo("An administrator account already exists.")

        click.echo("Application initialization complete.")

    def rebuild_rag_index():
        """Deletes and rebuilds the entire RAG vector index from all DataContext items."""
        with app.app_context():
            click.echo("Rebuilding RAG index. This may take a while...")
            all_contexts = DataContext.query.all()
            total = len(all_contexts)
            if total == 0:
                click.echo("No data contexts to index.")
                return
            
            # Clear existing index first (this now safely drops and re-creates tables)
            app.rag_service.delete_all_embeddings()
            
            with click.progressbar(all_contexts, length=total, label='Indexing contexts') as bar:
                for context in bar:
                    app.rag_service.generate_embeddings_for_context(context.id)
                    
            click.echo("Notifying addons to rebuild isolated vector stores...")
            app.addon_manager.notify_rag_rebuild()
            
            click.echo("RAG index rebuild complete.")

    if assistant_services:
        app.cli.command("rebuild-rag-index")(rebuild_rag_index)

    @app.cli.command("create-user")
    @click.argument("email")
    @click.argument("password")
    @click.option('--admin', is_flag=True, help="Make this user an administrator.")
    def create_user(email, password, admin):
        """Creates a new user with the given email and password."""
        with app.app_context():
            # If no users exist, make the first one an admin
            is_first_user = User.query.first() is None
            if is_first_user:
                admin = True
                print("No users found. Creating the first user as an administrator.")

            user = User.query.filter_by(email=email).first()
            if not user:
                user = User(email=email, is_admin=admin)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                print(f"User {email} created successfully{' as admin' if admin else ''}.")
            else:
                print(f"User {email} already exists.")


    @app.cli.command("grant-admin")
    @click.argument("email")
    def grant_admin(email):
        """Grants administrator privileges to a user."""
        with app.app_context():
            user = User.query.filter_by(email=email).first()
            if user:
                user.is_admin = True
                db.session.commit()
                print(f"Admin privileges granted to {email}.")
            else:
                print(f"User {email} not found.")

    @app.cli.command("init-db")
    def init_db_command():
        """Creates the database tables."""
        db.create_all()
        print("Initialized the database.")


    @click.option('--file', required=True, help='The path to the old SQLite database file to import from.')
    @click.option('--email', required=True, help='The email of the user to assign the imported data to.')
    def import_news_data(file, email):
        """Imports news pipeline data from an old database backup."""
        with app.app_context():
            from sqlalchemy import MetaData, Table, create_engine, select

            from .addons.news_pipeline.models import (
                NewsPipeline,
                NewsSource,
                ScrapedNewsItem,
            )

            target_user = User.query.filter_by(email=email).first()
            if not target_user:
                click.echo(f"Error: User with email '{email}' not found in the current database.")
                return

            old_db_path = Path(file)
            if not old_db_path.exists():
                click.echo(f"Error: Old database file not found at {file}")
                return

            click.echo(f"Connecting to old database at: {old_db_path}")
            old_engine = create_engine(f'sqlite:///{old_db_path}')
            old_meta = MetaData()
            
            try:
                with old_engine.connect() as old_conn:
                    # Reflect tables from the old database
                    t_pipeline = Table('news_pipeline', old_meta, autoload_with=old_engine)
                    t_source = Table('news_source', old_meta, autoload_with=old_engine)
                    t_item = Table('scraped_news_item', old_meta, autoload_with=old_engine)

                    # Fetch data as dictionaries
                    old_pipelines = old_conn.execute(select(t_pipeline)).mappings().all()
                    click.echo(f"Found {len(old_pipelines)} pipelines to import.")
                    
                    pipeline_id_map = {}
                    source_id_map = {}
                    imported_pipelines, imported_sources, imported_items = 0, 0, 0
                    
                    click.echo("Clearing existing news pipeline data for the target user...")
                    NewsPipeline.query.filter_by(user_id=target_user.id).delete()
                    db.session.commit()

                    for old_p_row in old_pipelines:
                        # Convert the immutable RowMapping to a mutable dict
                        old_p_dict = dict(old_p_row)
                        old_p_id = old_p_dict.get('id')
                        if not old_p_id:
                            continue

                        # Explicitly map fields to avoid errors from schema differences
                        new_p_data = {
                            'user_id': target_user.id,
                            'name': old_p_dict.get('name'),
                            'description': old_p_dict.get('description'),
                            'suitability_criteria': old_p_dict.get('suitability_criteria'),
                            'summarization_model': old_p_dict.get('summarization_model'),
                            'summarization_prompt': old_p_dict.get('summarization_prompt')
                        }
                        
                        new_p = NewsPipeline(**{k: v for k, v in new_p_data.items() if v is not None})
                        db.session.add(new_p)
                        db.session.flush() # Get the new ID
                        pipeline_id_map[old_p_id] = new_p.id
                        imported_pipelines += 1
                        
                        old_sources = old_conn.execute(select(t_source).where(t_source.c.pipeline_id == old_p_id)).mappings().all()
                        for old_s_row in old_sources:
                            old_s_dict = dict(old_s_row)
                            old_s_id = old_s_dict.get('id')
                            if not old_s_id:
                                continue

                            new_s_data = {
                                'pipeline_id': new_p.id,
                                'name': old_s_dict.get('name'),
                                'url': old_s_dict.get('url'),
                                'is_active': old_s_dict.get('is_active', True),
                                'is_aggregator': old_s_dict.get('is_aggregator', False),
                                'user_instructions': old_s_dict.get('user_instructions'),
                                'last_run_at': old_s_dict.get('last_run_at'),
                                'list_script': old_s_dict.get('list_script'),
                                'content_script': old_s_dict.get('content_script'),
                            }
                            
                            new_s = NewsSource(**{k: v for k, v in new_s_data.items() if v is not None})
                            db.session.add(new_s)
                            db.session.flush()
                            source_id_map[old_s_id] = new_s.id
                            imported_sources += 1

                            old_items = old_conn.execute(select(t_item).where(t_item.c.news_source_id == old_s_id)).mappings().all()
                            for old_i_row in old_items:
                                old_i_dict = dict(old_i_row)
                                # Avoid duplicates based on the unique URL constraint
                                if old_i_dict.get('url') and ScrapedNewsItem.query.filter_by(url=old_i_dict.get('url')).first():
                                    continue
                                
                                new_i_data = {
                                    'news_source_id': new_s.id,
                                    'url': old_i_dict.get('url'),
                                    'title': old_i_dict.get('title'),
                                    'content': old_i_dict.get('content'),
                                    'scraped_at': old_i_dict.get('scraped_at'),
                                    'status': old_i_dict.get('status', 'pending_review'),
                                    'suitability_score': old_i_dict.get('suitability_score'),
                                    'summary': old_i_dict.get('summary'),
                                    'reasoning': old_i_dict.get('reasoning'),
                                    'published_at': old_i_dict.get('published_at')
                                }
                                
                                new_i = ScrapedNewsItem(**{k: v for k, v in new_i_data.items() if v is not None})
                                db.session.add(new_i)
                                imported_items += 1
                    
                    db.session.commit()
                    click.echo(f"Successfully imported data for user '{email}':")
                    click.echo(f" - {imported_pipelines} pipelines")
                    click.echo(f" - {imported_sources} sources")
                    click.echo(f" - {imported_items} scraped items")

            except Exception as e:
                db.session.rollback()
                click.echo(f"An error occurred during import: {e}")
                click.echo("This may be due to schema differences between the old and new databases.")

    if assistant_services:
        app.cli.command("import-news-data")(import_news_data)

    @app.cli.group("addons")
    def addons_cli():
        """Manage addons."""
        pass

    @addons_cli.command("build-assets")
    @click.argument("addon_id", required=False)
    @click.option("--all", "build_all", is_flag=True, help="Build assets for all addons with frontend.build declarations.")
    @click.option("--watch", is_flag=True, help="Run the addon's declared frontend watch command.")
    @click.option("--force", is_flag=True, help="Build even when the current build stamp is up to date.")
    def addons_build_assets(addon_id, build_all, watch, force):
        """Build declared compiled frontend assets for addons."""
        if build_all and addon_id:
            raise click.ClickException("Use either an addon_id or --all, not both.")
        if not build_all and not addon_id:
            raise click.ClickException("Provide an addon_id or use --all.")
        if build_all and watch:
            raise click.ClickException("--watch can only be used with one addon_id.")
        if not hasattr(app, 'addon_manager'):
            raise click.ClickException("Addon manager is not initialized.")

        if build_all:
            results = app.addon_manager.build_all_frontend_assets(force=force)
            if not results:
                click.echo("No addons declare frontend builds.")
                return
            failures = []
            for result in results:
                status = result.get("status", {})
                label = "OK" if result.get("success") else "FAILED"
                click.echo(f"{label} {result.get('addon_id')}: {status.get('status')} - {result.get('message')}")
                if not result.get("success"):
                    failures.append(result)
            if failures:
                raise click.ClickException(f"{len(failures)} addon frontend build(s) failed.")
            return

        result = app.addon_manager.build_frontend_assets(addon_id, watch=watch, force=force)
        status = result.get("status", {})
        click.echo(f"{addon_id}: {status.get('status')} - {result.get('message')}")
        if not result.get("success"):
            raise click.ClickException(result.get("message") or "Frontend build failed.")

    @addons_cli.group("db")
    def addons_db_cli():
        """Manage addon database migrations."""
        pass
    
    @addons_db_cli.command("init")
    @click.option('--addon', required=True, help='The ID of the addon to initialize migrations for.')
    def addons_db_init(addon):
        """Initializes a new migration environment for an addon."""
        addon_dir = Path(app.root_path) / 'addons' / addon
        if not addon_dir.is_dir():
            click.echo(f"Error: Addon '{addon}' not found at {addon_dir}")
            return

        migrations_dir = addon_dir / 'migrations'
        if migrations_dir.exists():
            click.echo(f"Error: Migrations directory already exists for addon '{addon}'.")
            return

        click.echo(f"Initializing Alembic environment for addon '{addon}'...")

        try:
            from mako.template import Template
            
            # Create directories
            migrations_dir.mkdir()
            (migrations_dir / 'versions').mkdir()

            # Create alembic.ini from template
            template_dir = Path(app.root_path) / 'core' / 'alembic_templates'
            if not (template_dir / 'alembic.ini.mako').exists():
                 click.echo(f"Error: Alembic template directory not found at {template_dir}")
                 return
            
            ini_template = Template(filename=str(template_dir / 'alembic.ini.mako'))
            with open(migrations_dir / 'alembic.ini', 'w', encoding='utf-8') as f:
                f.write(ini_template.render(addon_id=addon))

            # Create env.py from template
            env_template = Template(filename=str(template_dir / 'env.py.mako'))
            with open(migrations_dir / 'env.py', 'w', encoding='utf-8') as f:
                f.write(env_template.render(addon_id=addon))
            
            # Copy script.py.mako
            shutil.copy(template_dir / 'script.py.mako', migrations_dir)

            # Create README
            with open(migrations_dir / 'README', 'w', encoding='utf-8') as f:
                f.write(f"This directory contains the Alembic database migrations for the '{addon}' addon.")

            click.echo("Alembic environment created successfully.")
            click.echo("You can now run 'flask addons db migrate' to create the initial migration.")

        except ImportError:
            click.echo("Error: 'mako' library not found. Please run 'pip install mako'.")
        except Exception as e:
            click.echo(f"An error occurred: {e}")
            if migrations_dir.exists():
                shutil.rmtree(migrations_dir)

    def _get_addon_alembic_config(addon_id):
        from alembic.config import Config as AlembicConfig

        addon_dir = Path(app.root_path) / 'addons' / addon_id
        migrations_dir = addon_dir / 'migrations'
        alembic_ini_path = migrations_dir / 'alembic.ini'

        if not alembic_ini_path.is_file():
            click.echo(f"Error: Addon '{addon_id}' does not have an Alembic migration environment. Run 'flask addons db init --addon {addon_id}' first.")
            return None

        alembic_cfg = AlembicConfig(str(alembic_ini_path))
        alembic_cfg.set_main_option('script_location', str(migrations_dir))
        alembic_cfg.set_main_option('sqlalchemy.url', app.config['SQLALCHEMY_DATABASE_URI'])
        return alembic_cfg

    @addons_db_cli.command("migrate")
    @click.option('--addon', required=True, help='The ID of the addon.')
    @click.option('-m', '--message', required=True, help='Revision message.')
    def addons_db_migrate(addon, message):
        """Creates a new migration file."""
        from alembic import command as alembic_command

        alembic_cfg = _get_addon_alembic_config(addon)
        if not alembic_cfg: return
        
        # Ensure versions directory exists, as manual scaffolding might miss it
        addon_dir = Path(app.root_path) / 'addons' / addon
        versions_dir = addon_dir / 'migrations' / 'versions'
        versions_dir.mkdir(parents=True, exist_ok=True)

        click.echo(f"Generating migration for '{addon}'...")
        alembic_command.revision(alembic_cfg, message=message, autogenerate=True)

    @addons_db_cli.command("upgrade")
    @click.option('--addon', required=True, help='The ID of the addon.')
    @click.option('--revision', default='head', help="The revision to upgrade to.")
    def addons_db_upgrade(addon, revision):
        """Upgrades the addon's database to a later version."""
        click.echo(f"Upgrading database for '{addon}' to revision '{revision}'...")
        addon_manager = getattr(app, "addon_manager", None)
        if addon_manager is None:
            raise click.ClickException("The add-on manager is unavailable.")
        try:
            with addon_manager.lifecycle_maintenance_authority():
                addon_manager.upgrade_addon_db(addon, revision)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    @addons_db_cli.command("downgrade")
    @click.option('--addon', required=True, help='The ID of the addon.')
    @click.option(
        '--revision',
        required=True,
        help="An explicit reviewed revision, or 'base', to downgrade to.",
    )
    def addons_db_downgrade(addon, revision):
        """Reverts the addon's database to a previous version."""
        click.echo(f"Downgrading database for '{addon}' to revision '{revision}'...")
        addon_manager = getattr(app, "addon_manager", None)
        if addon_manager is None:
            raise click.ClickException("The add-on manager is unavailable.")
        try:
            with addon_manager.lifecycle_maintenance_authority():
                addon_manager.downgrade_addon_db(addon, revision)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    @addons_db_cli.command("stamp")
    @click.option('--addon', required=True, help='The ID of the addon.')
    @click.option('--revision', required=True, help="The existing physical revision.")
    def addons_db_stamp(addon, revision):
        """Reconciles manager state with an existing physical migration ledger."""
        click.echo(f"Stamping database for '{addon}' with revision '{revision}'...")
        addon_manager = getattr(app, "addon_manager", None)
        if addon_manager is None:
            raise click.ClickException("The add-on manager is unavailable.")
        try:
            with addon_manager.lifecycle_maintenance_authority():
                addon_manager.stamp_addon_db(addon, revision)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("Stamp complete.")

    @addons_db_cli.command("adopt")
    @click.option('--addon', required=True, help='The ID of the addon.')
    @click.option(
        '--revision',
        default='head',
        show_default=True,
        help="The exact legacy schema revision to rehearse and adopt.",
    )
    def addons_db_adopt(addon, revision):
        """Back up, fingerprint, and adopt an exact unversioned legacy schema."""
        addon_manager = getattr(app, "addon_manager", None)
        if addon_manager is None:
            raise click.ClickException("The add-on manager is unavailable.")
        try:
            with addon_manager.lifecycle_maintenance_authority():
                result = addon_manager.adopt_addon_db(addon, revision)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"Adopted '{addon}' at {result['revision']} after verified backup "
            f"{result['backup_sha256']}."
        )

    @app.cli.command("generate-cert")
    def generate_cert_command():
        """Generates a self-signed certificate for development."""
        cert_file = Path(__file__).parent / 'cert.pem'
        key_file = Path(__file__).parent / 'key.pem'
        
        if cert_file.is_file() and key_file.is_file():
            print("Certificate files (cert.pem, key.pem) already exist. Skipping generation.")
            return

        print("Generating self-signed certificate for development...")
        try:
            from OpenSSL import crypto
            pkey = crypto.PKey()
            pkey.generate_key(crypto.TYPE_RSA, 2048)
            x509 = crypto.X509()
            x509.get_subject().CN = "localhost"
            x509.set_serial_number(1000)
            x509.gmtime_adj_notBefore(0)
            x509.gmtime_adj_notAfter(365 * 24 * 60 * 60) # 1 year
            x509.set_issuer(x509.get_subject())
            x509.set_pubkey(pkey)
            x509.sign(pkey, 'sha256')
            
            with open(cert_file, "wb") as f:
                f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, x509))
            with open(key_file, "wb") as f:
                f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, pkey))
            print(f"Certificate generated and saved to {cert_file} and {key_file}.")
        except ImportError:
            print("Error: pyOpenSSL is required to generate certificates. Please run 'pip install pyOpenSSL'.")
        except Exception as e:
            print(f"An error occurred during certificate generation: {e}")

    @app.cli.command("clear-migration-history")
    def clear_migration_history_command():
        """Manually deletes the alembic_version table to fix a broken migration state."""
        with app.app_context():
            from sqlalchemy import text
            try:
                with db.engine.connect() as connection, connection.begin():
                    connection.execute(text("DROP TABLE IF EXISTS alembic_version;"))
                click.echo("Successfully cleared migration history table (alembic_version).")
            except Exception as e:
                click.echo(f"An error occurred: {e}")

    @app.cli.command("reset-db")
    def reset_db_command():
        """
        Resets the database, preserving users, settings, credentials, and other key configurations.
        All transactional data like emails and chats will be deleted.
        """
        if click.confirm('Are you sure you want to reset the database? All non-essential data (chats, emails, etc.) will be lost.'):
            from flask_migrate import upgrade as migrate_upgrade

            from iarf_assistant.core.migration_runtime import migrations_directory

            with app.app_context():
                click.echo("Backing up preserved data...")
                
                # Core models to preserve
                core_models = [LLMProvider, User, Setting, PluginCredential, ApiKey, LLMModel, CredentialDelegation]
                # Addon models to preserve
                addon_models = app.addon_manager.collect_preserved_models()
                
                all_preserved_models = core_models + addon_models
                preserved_data = {}

                for model in all_preserved_models:
                    table_name = model.__tablename__
                    preserved_data[table_name] = [r.__dict__ for r in model.query.all()]
                    for record in preserved_data[table_name]:
                        record.pop('_sa_instance_state', None)

                click.echo("Dropping all tables...")
                db.drop_all()
                
                click.echo("Recreating all tables from migrations...")
                migrate_upgrade(directory=str(migrations_directory()))

                click.echo("Restoring preserved data...")
                
                # Restore core models first (order matters for FKs)
                restore_order_core = [
                    'llm_providers', 'users', 'settings', 'plugin_credentials', 
                    'api_keys', 'llm_models', 'credential_delegations'
                ]
                
                # Restore core tables
                model_map = {m.__tablename__: m for m in all_preserved_models}
                
                for table_name in restore_order_core:
                    if table_name in preserved_data:
                        model = model_map[table_name]
                        records = preserved_data[table_name]
                        if records:
                            for record_data in records:
                                db.session.add(model(**record_data))
                            db.session.commit()
                            click.echo(f"Restored {len(records)} records for {table_name}.")
                
                # Restore addon tables (order is less guaranteed here, assuming addons handle their own internal dependencies or order doesn't matter as much for these specific preserved models)
                for table_name, records in preserved_data.items():
                    if table_name not in restore_order_core and records:
                        model = model_map.get(table_name)
                        if model:
                            for record_data in records:
                                db.session.add(model(**record_data))
                            db.session.commit()
                            click.echo(f"Restored {len(records)} records for {table_name}.")

                click.echo("Database reset complete. Preserved data has been restored.")

    @app.cli.command("nuke-db")
    def nuke_db_command():
        """Deletes the database file for a completely fresh start. All data will be lost."""
        db_path = Path(app.instance_path) / 'iarf_ai.db'
        if not db_path.exists():
            click.echo("Database file not found. Nothing to do.")
            return

        if click.confirm(f"This will permanently delete the database file at {db_path}. This cannot be undone. Are you sure?"):
            try:
                # To be safe, also delete the session files as they might be invalid
                session_dir = Path(app.instance_path) / 'sessions'
                if session_dir.exists():
                    shutil.rmtree(session_dir)
                    click.echo("Deleted session directory.")
                
                os.remove(db_path)
                click.echo("Database file deleted.")
                click.echo("Run 'flask init-app' to create a new database with the correct schema and default data.")
            except Exception as e:
                click.echo(f"Error deleting database file: {e}")
