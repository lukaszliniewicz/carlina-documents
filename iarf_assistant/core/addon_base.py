from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONNECTION_UNSET = object()


@dataclass(frozen=True, slots=True)
class CapabilityProviderHandle:
    """A resolved provider plus its optional user-scoped Connection."""

    requirement_id: str
    consumer_addon_id: str
    provider_addon_id: str
    provider_capability_id: str
    connection_id: str | None
    operations: tuple[str, ...]
    addon: Any
    user_id: int

    def initialize_client(self, config: dict[str, Any] | None = None):
        """Initialize the selected provider without leaking its secret payload."""

        resolved = self._config(config)
        return self.addon.initialize_client(resolved)

    def _config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Build request-local routing config for this selected provider."""

        resolved = dict(config or {})
        resolved.setdefault("current_user_id", self.user_id)
        resolved.setdefault("capability_requirement_id", self.requirement_id)
        resolved.setdefault("capability_consumer_addon_id", self.consumer_addon_id)
        resolved.setdefault("capability_provider_addon_id", self.provider_addon_id)
        resolved.setdefault("capability_provider_id", self.provider_capability_id)
        if self.connection_id:
            resolved.setdefault("connection_id", self.connection_id)
        return resolved

    def get_tool_declarations(
        self,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return reviewed tool declarations owned by the selected provider."""

        return self.addon.context.get_tool_declarations(self._config(config))

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        config: dict[str, Any] | None = None,
        *,
        runtime_policy: dict[str, Any] | None = None,
        approval_id: str | None = None,
        resource_reference_ids: list[str] | None = None,
    ):
        """Execute a selected provider's tool through the core execution kernel."""

        return self.addon.context.execute_tool(
            tool_name,
            arguments,
            self._config(config),
            runtime_policy=runtime_policy,
            approval_id=approval_id,
            resource_reference_ids=resource_reference_ids,
        )


class AddonContext:
    """Provides trusted add-ons with stable Carlina APIs and owned paths.

    This is a compatibility boundary, not a Python sandbox. Native add-ons run
    with the host process's effective access.
    """

    def __init__(self, app, addon_manager, addon_id: str | None = None):
        self._app = app
        self._addon_manager = addon_manager
        self.addon_id = addon_id

    @property
    def application(self):
        """Return the trusted host application for lifecycle integration.

        Native add-ons are trusted Python extensions rather than sandboxed
        plugins. This explicit escape hatch keeps the dependency reviewable
        while avoiding private-attribute coupling in lifecycle code.
        """

        return self._app

    @property
    def scheduler(self):
        """Return the host scheduler used for declared background jobs."""

        return self._app.scheduler

    def app_context(self):
        """Create a Flask application context for lifecycle callbacks."""

        return self._app.app_context()

    def test_request_context(self, *args, **kwargs):
        """Create an internal request context for legacy route adapters."""

        return self._app.test_request_context(*args, **kwargs)

    def get_plugin(self, plugin_id: str):
        """Retrieves a loaded plugin addon instance by its ID."""
        return self._addon_manager.plugins.get(plugin_id)

    def get_addon(self, addon_id: str):
        """Retrieves any loaded addon instance by its ID, regardless of type."""
        return self._addon_manager.get_loaded_addon(addon_id)

    def get_addon_contract(self, addon_id: str):
        """Return immutable metadata without importing another add-on."""

        return self._addon_manager.get_addon_contract(addon_id)

    def get_provider_records(
        self, domain: str | None = None, include_inactive: bool = False
    ):
        """Retrieves provider records from the provider registry."""
        return self._addon_manager.get_provider_records(
            domain, include_inactive=include_inactive
        )

    def get_provider_addon(self, domain: str, provider_id: str | None = None):
        """Retrieves the loaded addon instance that owns a provider record."""
        return self._addon_manager.get_provider_addon(domain, provider_id)

    def get_helper_record(self, helper_id: str):
        """Resolves a helper by stable id or compatibility alias."""
        return self._addon_manager.get_helper_record(helper_id)

    def get_ui_component_record(self, component_id: str):
        """Resolves a UI component by stable id or compatibility alias."""
        return self._addon_manager.get_ui_component_record(component_id)

    def get_tool_record(self, tool_id: str):
        """Resolves a tool by stable id or legacy function-call binding."""
        return self._addon_manager.get_tool_record(tool_id)

    def get_tool_declarations(
        self,
        config: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return this add-on's registry-owned tool declarations.

        Add-on-owned agent loops use this method instead of reaching into the
        manager's registries or constructing declarations from private
        function mappings.
        """

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        return self._addon_manager.get_tool_declarations_for_plugin_ids(
            [self.addon_id],
            dict(config or {}),
        )

    def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        config: dict[str, Any] | None = None,
        *,
        runtime_policy: dict[str, Any] | None = None,
        approval_id: str | None = None,
        resource_reference_ids: list[str] | None = None,
    ):
        """Execute one of this add-on's tools through the execution kernel."""

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        record = self.get_tool_record(tool_name)
        if record is None or record.addon_id != self.addon_id:
            raise PermissionError(
                f"Tool {tool_name!r} is not owned by add-on {self.addon_id!r}."
            )
        resolved_config = dict(config or {})
        user_id = resolved_config.get("current_user_id")
        if user_id is None:
            raise ValueError("current_user_id is required to execute an add-on tool.")
        return self._addon_manager.execute_capability(
            tool_name,
            arguments,
            resolved_config,
            requester_user_id=int(user_id),
            runtime_policy=runtime_policy,
            approval_id=approval_id,
            resource_reference_ids=resource_reference_ids,
            audit_log_source_id=f"addons.{self.addon_id}",
            log_source_ids=(f"addons.{self.addon_id}",),
        )

    def get_skill_record(self, skill_id: str):
        """Resolves a skill by stable id."""
        return self._addon_manager.get_skill_record(skill_id)

    def get_workflow_record(self, workflow_id: str):
        """Resolves a workflow definition by stable id."""
        return self._addon_manager.get_workflow_record(workflow_id)

    def get_prompt_record(
        self,
        prompt_id: str,
        *,
        addon_id: str | None = None,
    ):
        """Resolve declared prompt metadata without reaching into registries."""

        owner_id = str(addon_id or self.addon_id or "").strip()
        if not owner_id:
            raise RuntimeError("An add-on ID is required to resolve a prompt.")
        return self._addon_manager.get_prompt_record(prompt_id, addon_id=owner_id)

    def get_prompt_content(
        self,
        prompt_id: str,
        *,
        addon_id: str | None = None,
    ) -> str:
        """Return required prompt content through the stable add-on API."""

        owner_id = str(addon_id or self.addon_id or "").strip()
        record = self.get_prompt_record(prompt_id, addon_id=owner_id)
        if record is None or not str(record.content or "").strip():
            raise LookupError(
                f"Add-on {owner_id!r} has no populated prompt {prompt_id!r}."
            )
        return str(record.content)

    def get_capabilities(self, include_inactive: bool = False):
        """Returns user-facing capabilities projected from active addons."""
        return self._addon_manager.get_capability_manifest(
            include_inactive=include_inactive
        )

    def capability_candidates(
        self,
        requirement_id: str,
        *,
        include_inactive: bool = False,
    ):
        """Return compatible catalog candidates for declared metadata discovery.

        This does not select a provider for an operation and therefore does not
        bypass user-scoped bindings or Connections. It is intended for catalog
        surfaces such as Audio Core's engine/model inventory.
        """

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        from .addons.capabilities import CapabilityCatalog
        from .addons.capability_runtime import resolved_contracts_from_manager

        contracts = resolved_contracts_from_manager(self._addon_manager)
        consumer = contracts.get(self.addon_id)
        if consumer is None:
            raise LookupError(f"Add-on {self.addon_id!r} has no resolved contract.")
        requirement = next(
            (
                item
                for item in consumer.capability_requirements
                if item.id == requirement_id
            ),
            None,
        )
        if requirement is None:
            raise LookupError(
                f"Add-on {self.addon_id!r} has no capability requirement "
                f"{requirement_id!r}."
            )
        eligible = (
            tuple(contracts.values())
            if include_inactive
            else tuple(
                contract
                for addon_id, contract in contracts.items()
                if addon_id in self._addon_manager.loaded_addons
            )
        )
        return tuple(
            match.candidate
            for match in CapabilityCatalog(eligible).matches(requirement)
            if match.compatible
        )

    def resolve_capabilities(self, user_id: int):
        """Resolve this add-on's declared capability requirements for one user."""

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        from iarf_assistant.extensions import db

        from .addons.capability_runtime import (
            resolve_persisted_capabilities,
            resolved_contracts_from_manager,
        )

        return resolve_persisted_capabilities(
            db.session,
            contracts=resolved_contracts_from_manager(self._addon_manager),
            consumer_addon_id=self.addon_id,
            user_id=int(user_id),
        )

    def require_capability(self, requirement_id: str, user_id: int):
        """Resolve a declared first-use requirement or raise its setup state."""

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        from iarf_assistant.extensions import db

        from .addons.capability_runtime import (
            require_persisted_capability,
            resolved_contracts_from_manager,
        )

        return require_persisted_capability(
            db.session,
            contracts=resolved_contracts_from_manager(self._addon_manager),
            consumer_addon_id=self.addon_id,
            requirement_id=requirement_id,
            user_id=int(user_id),
        )

    def get_capability_provider(
        self,
        requirement_id: str,
        user_id: int,
        *,
        selection: int = 0,
        provider_addon_id: str | None = None,
        provider_capability_id: str | None = None,
        connection_id: str | None = None,
    ) -> CapabilityProviderHandle:
        """Return an executable handle for a resolved capability provider."""

        explicit_connection = None
        explicit_provider = bool(provider_addon_id or provider_capability_id)
        if connection_id:
            from iarf_assistant.extensions import db

            from .addons.connections import ConnectionService

            explicit_connection = next(
                (
                    item
                    for item in ConnectionService(db.session).visible_to_user(user_id)
                    if item.id == str(connection_id)
                ),
                None,
            )
            if explicit_connection is None:
                raise PermissionError(
                    f"Connection {connection_id!r} is not visible to this user."
                )
            if explicit_connection.state != "ready":
                raise RuntimeError(
                    f"Connection {connection_id!r} is "
                    f"{explicit_connection.state!r}, not ready."
                )
            persisted = self.resolve_capabilities(user_id)
            resolution = next(
                (
                    item
                    for item in persisted.report.resolutions
                    if item.requirement.id == requirement_id
                ),
                None,
            )
            if resolution is None:
                raise LookupError(
                    f"Add-on {self.addon_id!r} has no capability requirement "
                    f"{requirement_id!r}."
                )
            candidate_pool = resolution.compatible
        elif explicit_provider:
            persisted = self.resolve_capabilities(user_id)
            resolution = next(
                (
                    item
                    for item in persisted.report.resolutions
                    if item.requirement.id == requirement_id
                ),
                None,
            )
            if resolution is None:
                raise LookupError(
                    f"Add-on {self.addon_id!r} has no capability requirement "
                    f"{requirement_id!r}."
                )
            # A deliberate operation-local provider choice may select any
            # compatible non-connector provider without changing the saved
            # default binding. Connector providers still require a visible
            # Connection below.
            candidate_pool = resolution.compatible
        else:
            resolution = self.require_capability(requirement_id, user_id)
            candidate_pool = resolution.selected
        candidates = []
        for candidate in candidate_pool:
            binding = next(
                (
                    item
                    for item in resolution.selected_bindings
                    if item.provider_addon_id == candidate.addon_id
                    and item.provider_capability_id == candidate.provider.id
                ),
                None,
            )
            if provider_addon_id and candidate.addon_id != provider_addon_id:
                continue
            if (
                provider_capability_id
                and candidate.provider.id != provider_capability_id
            ):
                continue
            if explicit_connection is not None and (
                explicit_connection.connector_addon_id != candidate.addon_id
                or candidate.provider.connector_definition
                != explicit_connection.connector_definition_id
            ):
                continue
            candidates.append((candidate, binding))
        try:
            candidate, binding = candidates[selection]
        except IndexError as exc:
            raise LookupError(
                f"Capability {requirement_id!r} has no matching provider at "
                f"selection {selection}."
            ) from exc
        selected_connection_id = (
            explicit_connection.id
            if explicit_connection is not None
            else (binding.connection_id if binding is not None else None)
        )
        if candidate.provider.connector_definition and not selected_connection_id:
            raise PermissionError(
                f"Provider {candidate.addon_id!r}/{candidate.provider.id!r} "
                "requires an explicit or bound Connection."
            )
        addon = self.get_addon(candidate.addon_id)
        if addon is None:
            raise RuntimeError(
                f"Resolved provider add-on {candidate.addon_id!r} is not loaded."
            )
        return CapabilityProviderHandle(
            requirement_id=requirement_id,
            consumer_addon_id=str(self.addon_id),
            provider_addon_id=candidate.addon_id,
            provider_capability_id=candidate.provider.id,
            connection_id=selected_connection_id,
            operations=candidate.provider.operations,
            addon=addon,
            user_id=int(user_id),
        )

    def visible_connections(
        self,
        user_id: int,
        *,
        connector_addon_id: str | None = None,
    ):
        """List Connections visible to a user without exposing their secrets."""

        from iarf_assistant.extensions import db

        from .addons.connections import ConnectionService

        return ConnectionService(db.session).visible_to_user(
            int(user_id),
            connector_addon_id=connector_addon_id,
        )

    def secret_broker(self, user_id: int):
        """Return the policy-enforcing secret gateway for this provider add-on."""

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        from iarf_assistant.extensions import db

        from .addons.connections import SecretBroker

        return SecretBroker(
            db.session,
            actor_user_id=int(user_id),
            requester_addon_id=self.addon_id,
        )

    def get_task_queue(self, addon_id: str):
        """
        Returns a TaskQueue instance bound to the specific addon.
        Used to enqueue jobs and check status from the Flask/API side.
        """
        from .task_queue import TaskQueue

        return TaskQueue(self._app.instance_path, addon_id)

    def get_container_url(self, addon_id: str | None = None) -> str:
        """
        Dynamically boots (if necessary) and returns the base URL
        for an addon's Docker microservice.
        """
        resolved_addon_id = addon_id or self.addon_id
        if not resolved_addon_id:
            raise ValueError("Addon ID is required to resolve a container URL.")
        return self._addon_manager.get_container_url(resolved_addon_id)

    def container_use(self, addon_id: str | None = None, *, timeout: float = 600):
        """Return a cross-process use guard for a bounded service request."""
        resolved_addon_id = addon_id or self.addon_id
        if not resolved_addon_id:
            raise ValueError("Addon ID is required to guard container use.")
        return self._addon_manager.container_use(resolved_addon_id, timeout=timeout)

    def keep_container_alive(self, addon_id: str | None = None) -> None:
        """Prevent a container used by an active operation from being reaped."""

        resolved_addon_id = addon_id or self.addon_id
        if not resolved_addon_id:
            raise ValueError("Addon ID is required to keep a container active.")
        self._addon_manager.touch_container_access(resolved_addon_id)

    def reset_container(self, addon_id: str | None = None) -> None:
        """Reset an add-on container stack after a recoverable service crash."""

        resolved_addon_id = addon_id or self.addon_id
        if not resolved_addon_id:
            raise ValueError("Addon ID is required to reset a container.")
        self._addon_manager.reset_container(resolved_addon_id)

    def run_managed_process(
        self,
        argv,
        *,
        runtime_unit: str | None = None,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ):
        """Run a command in this add-on's supervised Pixi environment."""

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        return self._addon_manager.run_managed_process(
            self.addon_id,
            argv,
            runtime_unit=runtime_unit,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            check=check,
        )

    def ensure_worker_pool(
        self,
        script_name: str,
        *,
        num_workers: int = 1,
        execution_environment: str | None = None,
        allow_environment_fallback: bool | None = None,
    ):
        """Start this add-on's declared supervised worker pool."""

        if not self.addon_id:
            raise RuntimeError("This context is not bound to an add-on ID.")
        options: dict[str, Any] = {}
        if execution_environment is not None:
            options["execution_environment"] = execution_environment
        if allow_environment_fallback is not None:
            options["allow_environment_fallback"] = allow_environment_fallback
        return self._addon_manager.spawn_managed_workers(
            self.addon_id,
            script_name,
            num_workers=max(1, int(num_workers)),
            **options,
        )

    def upgrade_database(self, revision: str = "head"):
        """Apply this add-on's declared Alembic migrations."""

        if not self.addon_id:
            raise RuntimeError("This context is not bound to an add-on ID.")
        return self._addon_manager.upgrade_addon_db(self.addon_id, revision)

    def mark_container_image_stale(self, version: str | None = None):
        """Schedule this add-on's container image for a lazy rebuild."""

        if not self.addon_id:
            raise RuntimeError("This context is not bound to an add-on ID.")
        return self._addon_manager.mark_docker_image_stale(self.addon_id, version)

    def _addon_directory(self, parent: Path, leaf: str | None = None) -> Path:
        """Return an owned, add-on-scoped mutable directory.

        Installed add-on code can be read-only. Add-ons therefore receive
        storage through this context instead of deriving paths from Flask's
        package root.
        """

        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        target = (parent / self.addon_id).resolve()
        if not target.is_relative_to(parent.resolve()):
            raise ValueError(f"Unsafe add-on ID: {self.addon_id!r}")
        if leaf:
            target /= leaf
        target.mkdir(parents=True, exist_ok=True)
        return target

    @property
    def data_dir(self) -> Path:
        """Persistent private data owned by this add-on."""
        paths = self._app.extensions["carlina_paths"]
        if not self.addon_id:
            raise RuntimeError("This add-on context is not bound to an add-on ID.")
        legacy = (paths.addons_dir / str(self.addon_id) / "data").resolve()
        canonical = (paths.addon_data_dir / str(self.addon_id)).resolve()
        if legacy.is_dir() and not canonical.exists():
            return legacy
        return self._addon_directory(paths.addon_data_dir)

    @property
    def code_dir(self) -> Path:
        """Read-only package directory selected by the add-on manager."""

        return self._addon_manager.get_addon_directory(self.addon_id)

    @property
    def environment_dir(self) -> Path:
        """Rebuildable dependency environment owned by this add-on."""

        paths = self._app.extensions["carlina_paths"]
        return self._addon_directory(paths.addon_environments_dir)

    @property
    def logs_dir(self) -> Path:
        """Rotating logs owned by this add-on."""

        paths = self._app.extensions["carlina_paths"]
        return self._addon_directory(paths.addon_logs_dir)

    @property
    def cache_dir(self) -> Path:
        """Rebuildable private cache owned by this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].cache_dir / "addons"
        )

    @property
    def temp_dir(self) -> Path:
        """Temporary private workspace owned by this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].temp_dir / "addons"
        )

    @property
    def artifacts_dir(self) -> Path:
        """Persistent generated artifacts owned by this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].artifacts_dir / "addons"
        )

    @property
    def uploads_dir(self) -> Path:
        """User-facing upload storage scoped to this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].uploads_dir / "addons"
        )

    @property
    def state_dir(self) -> Path:
        """Persistent operational state owned by this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].state_dir / "addons"
        )

    @property
    def locks_dir(self) -> Path:
        """Ephemeral cross-process locks owned by this add-on."""

        return self._addon_directory(
            self._app.extensions["carlina_paths"].locks_dir / "addons"
        )

    def get_vector_store(
        self,
        namespace: str,
        dimensions: int = 768,
        model: str = "openai/text-embedding-3-small",
        task_type: str = "search",
    ):
        """
        Returns an isolated VectorStore instance for the addon.
        The table will be prefixed with the addon ID automatically to prevent collisions.
        """
        if not hasattr(self._app, "rag_service"):
            return None

        from .addons.storage import addon_storage_key

        safe_addon_id = addon_storage_key(self.addon_id or "unknown")
        full_namespace = f"addon_{safe_addon_id}_{namespace}"

        return self._app.rag_service.create_isolated_store(
            namespace=full_namespace,
            dimensions=dimensions,
            model=model,
            task_type=task_type,
        )

    @property
    def login_required(self):
        # Pass-through for Flask-Login decorator
        from flask_login import login_required

        return login_required

    # ... other properties to securely expose app features ...


class AddonBase:
    """The base class that all addon entry point classes must inherit from."""

    def __init__(self, context: AddonContext, manifest: dict):
        self.context = context
        self.manifest = manifest
        self.id = manifest.get("id")

    @staticmethod
    def _capability_user_id(config: dict[str, Any] | None) -> int:
        """Return the user identity required by a scoped capability lookup."""

        user_id = (config or {}).get("current_user_id")
        if user_id is None:
            raise ValueError(
                "current_user_id is required to resolve a user-scoped capability."
            )
        return int(user_id)

    @staticmethod
    def child_capability_config(
        requirement_id: str,
        config: dict[str, Any] | None,
        *,
        connection_id: str | None | object = _CONNECTION_UNSET,
    ) -> dict[str, Any]:
        """Prepare configuration for a capability composed by this add-on.

        A provider receives routing metadata for the capability through which
        *it* was selected. Reusing that metadata for one of its own
        requirements can accidentally ask the child resolver for the outer
        provider or pass the outer connector's Connection. A nested request
        therefore starts with neutral routing unless the caller explicitly
        supplies the child Connection.
        """

        resolved = dict(config or {})
        routed_requirement = str(
            resolved.get("capability_requirement_id") or ""
        ).strip()
        if routed_requirement and routed_requirement != requirement_id:
            resolved.pop("capability_provider_addon_id", None)
            resolved.pop("capability_provider_id", None)
            resolved.pop("connection_id", None)
        resolved["capability_requirement_id"] = requirement_id
        if connection_id is not _CONNECTION_UNSET:
            if connection_id:
                resolved["connection_id"] = str(connection_id)
            else:
                resolved.pop("connection_id", None)
        return resolved

    def capability_provider(
        self,
        requirement_id: str,
        config: dict[str, Any] | None,
        *,
        selection: int = 0,
    ) -> CapabilityProviderHandle:
        """Resolve one declared provider for an operational add-on call."""

        resolved = self.child_capability_config(requirement_id, config)
        return self.context.get_capability_provider(
            requirement_id,
            self._capability_user_id(resolved),
            selection=selection,
            provider_addon_id=resolved.get("capability_provider_addon_id"),
            provider_capability_id=resolved.get("capability_provider_id"),
            connection_id=resolved.get("connection_id"),
        )

    def initialize_capability_client(
        self,
        requirement_id: str,
        config: dict[str, Any] | None,
        *,
        selection: int = 0,
    ):
        """Resolve and initialize a selected provider through its Connection."""

        resolved = self.child_capability_config(requirement_id, config)
        return self.capability_provider(
            requirement_id,
            resolved,
            selection=selection,
        ).initialize_client(resolved)

    def connection_record(
        self,
        config: dict[str, Any] | None,
        *,
        connector_definition_id: str,
    ):
        """Resolve and validate this connector's selected Connection record."""

        resolved = dict(config or {})
        connection_id = str(resolved.get("connection_id") or "").strip()
        if not connection_id:
            if not hasattr(self.context, "visible_connections"):
                return None
            user_id = self._capability_user_id(resolved)
            candidates = tuple(
                item
                for item in self.context.visible_connections(
                    user_id,
                    connector_addon_id=self.id,
                )
                if item.connector_definition_id == connector_definition_id
                and item.state == "ready"
            )
            defaults = tuple(item for item in candidates if item.is_default)
            selected = (
                defaults[0]
                if defaults
                else (candidates[0] if len(candidates) == 1 else None)
            )
            if selected is None:
                return None
            connection_id = str(selected.id)
            resolved["connection_id"] = connection_id
        user_id = self._capability_user_id(resolved)

        from iarf_assistant.extensions import db

        from .addons.connections import (
            ConnectionNotFound,
            ConnectionService,
            SecretAccessDenied,
        )

        try:
            connection = ConnectionService(db.session).require_visible(
                connection_id,
                user_id=user_id,
            )
        except ConnectionNotFound as exc:
            raise LookupError(
                f"Connection {connection_id!r} does not exist."
            ) from exc
        except SecretAccessDenied as exc:
            raise PermissionError(
                f"Connection {connection_id!r} is not visible to this user."
            ) from exc
        if connection.connector_addon_id != self.id:
            raise PermissionError(
                f"Connection {connection_id!r} belongs to connector "
                f"{connection.connector_addon_id!r}, not {self.id!r}."
            )
        if connection.connector_definition_id != connector_definition_id:
            raise ValueError(
                f"Connection {connection_id!r} does not implement "
                f"{connector_definition_id!r}."
            )
        if connection.state != "ready":
            raise RuntimeError(
                f"Connection {connection_id!r} is {connection.state!r}, not ready."
            )
        return connection

    def connection_payload(
        self,
        config: dict[str, Any] | None,
        *,
        connector_definition_id: str,
        secret_name: str = "credentials",
    ) -> dict[str, Any] | None:
        """Read this connector's selected Connection through the Secret Broker.

        Non-secret Connection configuration and its sealed payload are combined
        only inside the connector package. Consumers receive an initialized
        client, never this dictionary.
        """

        resolved = dict(config or {})
        connection = self.connection_record(
            resolved,
            connector_definition_id=connector_definition_id,
        )
        if connection is None:
            return None
        user_id = self._capability_user_id(resolved)

        payload = (
            dict(connection.config_json)
            if isinstance(connection.config_json, dict)
            else {}
        )
        payload.update(
            self.context.secret_broker(user_id).read(
                connection.id,
                name=secret_name,
            )
        )
        return payload

    def on_activate(self):
        """
        Called after declared migrations complete and before registry exposure.
        """

    def on_deactivate(self):
        """
        Optional method called when the addon is deactivated by an admin.
        This can be used for cleanup, but should not be destructive (e.g., don't drop tables).
        """

    def on_upgrade(self, from_version: str):
        """
        Called when the addon's version in its manifest is newer than in the database.
        Declared database migrations have already completed. Docker-backed
        add-ons are marked for image refresh, deferred until first use.

        :param from_version: The version string of the previously loaded addon.
        """
        if self.manifest.get("docker"):
            self.context.mark_container_image_stale(self.manifest.get("version"))

    def on_uninstall(self):
        """
        Deprecated compatibility hook. Code removal no longer calls this hook
        because removal must preserve add-on data and schema by default.
        """

    def on_purge_data(self):
        """Optional destructive cleanup hook called only after explicit purge."""

    def on_register_jobs(self, scheduler):
        """
        Called on application startup if the addon is active.
        Use this to register background jobs (e.g., cron tasks).
        IMPORTANT: Prefix your job IDs with your addon's ID (e.g., id=f"{self.id}_sync")
        so they can be cleanly removed when the addon is deactivated.
        """

    def on_settings_updated(self):
        """
        Called when the addon's settings are updated via the UI.
        Use this to dynamically reschedule jobs or reload configurations.
        """

    def is_configured(self) -> bool:
        """
        Determines if the addon has the necessary global configuration (API keys, etc.)
        to be used as a provider-level credential.
        """
        return False

    def on_encrypt_user_data(self, user):
        """
        Called when the user requests data encryption.
        The addon should iterate its own models and flag sensitive fields as modified.
        """

    def get_preserved_models(self) -> list:
        """
        Returns a list of SQLAlchemy model classes that should be preserved during a database reset
        or included in a backup.
        """
        return []

    def resolve_short_url(self, code: str, user) -> str | None:
        """
        Returns the long URL if the addon recognizes the short code, otherwise None.
        """
        return None

    def get_knowledge_provider(self, source_type: str):
        """
        Returns an object capable of handling knowledge base operations (syncing, exporting)
        for specific source types.
        """
        return

    def on_rag_rebuild_triggered(self):
        """
        Called automatically when the user triggers a global RAG index rebuild.
        Addons that use `IsolatedVectorStore` MUST override this method to drop
        their old tables and re-embed their data to sync with new system dimensions/models.
        """
