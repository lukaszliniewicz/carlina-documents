from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .frontend_builds import frontend_build_declared, validate_frontend_tier


ACTIVE = "active"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
MISCONFIGURED = "misconfigured"
MISSING_DEPENDENCY = "missing_dependency"
INACTIVE = "inactive"

VALID_ELEMENT_STATUSES = {
    ACTIVE,
    DEGRADED,
    UNAVAILABLE,
    MISCONFIGURED,
    MISSING_DEPENDENCY,
    INACTIVE,
}

AVAILABLE_ELEMENT_STATUSES = {ACTIVE, DEGRADED}
RUNTIME_CONTROL_ARGUMENTS = {"_approval_id", "_resource_reference_ids"}

LEGACY_ADAPTER = "legacy_adapter"
REPRESENTATIVE_MIGRATED = "representative_migrated"
EXPLICIT_MIGRATED = "explicit_migrated"
DEFERRED = "deferred"
RETIRED = "retired"

VALID_MIGRATION_STATUSES = {
    LEGACY_ADAPTER,
    REPRESENTATIVE_MIGRATED,
    EXPLICIT_MIGRATED,
    DEFERRED,
    RETIRED,
}

STRICT_EXPLICIT_LEGACY_FIELDS = {
    "addon_type",
    "plugin_prompt",
    "automation_prompts",
    "helpers",
    "ui_entry_points",
    "ui_surfaces",
}

PORTABLE_SKILL_FRONTMATTER_KEYS = {
    "name",
    "description",
    "version",
    "author",
    "license",
    "required_addons",
    "required_tools",
    "required_tool_views",
    "required_helpers",
    "required_ui_components",
    "required_providers",
    "required_services",
    "required_workers",
    "auth_scopes",
    "resource_scopes",
    "approval_gates",
    "tools",
    "tool_ids",
    "tool_views",
    "tool_view_ids",
    "helpers",
    "helper_ids",
    "ui_components",
    "ui_component_ids",
    "providers",
    "provider_ids",
    "services",
    "service_ids",
    "workers",
    "worker_ids",
    "prompts",
    "prompt_ids",
    "metadata",
}


def _normalize_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return [value]


def _normalize_record_list(value: Any) -> list[dict]:
    if not value:
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        records = []
        for key, item in value.items():
            if isinstance(item, dict):
                record = dict(item)
                record.setdefault("id", key)
                records.append(record)
        return records
    return []


def _normalize_named_record_list(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    if isinstance(value, list):
        records = []
        for item in value:
            if isinstance(item, dict):
                records.append(dict(item))
            elif item:
                records.append({"id": str(item)})
        return records
    if isinstance(value, dict):
        records = []
        for key, item in value.items():
            if isinstance(item, dict):
                record = dict(item)
                record.setdefault("id", key)
            else:
                record = {"id": key, "value": item}
            records.append(record)
        return records
    return [{"id": str(value)}]


def _normalize_string_list(value: Any) -> list[str]:
    return [str(item) for item in _normalize_list(value) if item]


def _merge_unique(*values: Any) -> list[str]:
    seen = set()
    merged = []
    for value in values:
        for item in _normalize_string_list(value):
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged


def _truthy_manifest_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _parse_skill_markdown(skill_file: Path) -> tuple[dict[str, Any], str]:
    if skill_file.stat().st_size > 1_048_576:
        raise ValueError("SKILL.md exceeds the 1 MiB contract limit.")
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text.strip()

    match = re.match(r"\A---\r?\n(?P<header>.*?)\r?\n---(?:\r?\n|\Z)", text, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md has unterminated YAML frontmatter.")
    header = match.group("header")
    if len(header.encode("utf-8")) > 65_536:
        raise ValueError("SKILL.md YAML frontmatter exceeds 64 KiB.")
    parsed = yaml.safe_load(header) if header.strip() else {}
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError("SKILL.md YAML frontmatter must be an object.")
    frontmatter = {str(key): value for key, value in parsed.items()}
    return frontmatter, text[match.end():].strip()


def _safe_relative_path(base_dir: Path, relative_path: str) -> Path:
    candidate = (base_dir / relative_path).resolve()
    candidate.relative_to(base_dir.resolve())
    return candidate


def _qualified_id(addon_id: str, value: str | None, default: str = "default") -> str:
    raw = str(value or default).strip().replace(" ", "_")
    if not raw:
        raw = default
    if "." in raw:
        return raw
    return f"{addon_id}.{raw}"


def _strip_legacy_prefix(function_name: str, prefix: str | None, addon_id: str) -> str:
    for candidate in (prefix, addon_id):
        if candidate and function_name.startswith(f"{candidate}_"):
            return function_name[len(candidate) + 1:]
    return function_name


def _safe_id(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    return value.strip("_") or "default"


def _status_for_active_state(is_active: bool) -> "ElementStatus":
    if is_active:
        return ElementStatus(ACTIVE)
    return ElementStatus(INACTIVE, ["Addon is inactive."])


def _missing_asset_reasons(addon_dir: Path | None, assets: list[str]) -> list[str]:
    if not addon_dir:
        return []
    reasons = []
    for asset in assets:
        if not asset:
            continue
        asset_path = addon_dir / asset
        try:
            asset_path.relative_to(addon_dir)
        except ValueError:
            reasons.append(f"Declared asset escapes addon directory: {asset}")
            continue
        if not asset_path.exists():
            reasons.append(f"Declared asset is missing: {asset}")
    return reasons


@dataclass
class ElementStatus:
    state: str = ACTIVE
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.state not in VALID_ELEMENT_STATUSES:
            self.reasons.append(f"Unknown status '{self.state}' normalized to unavailable.")
            self.state = UNAVAILABLE

    def with_reasons(self, state: str, reasons: list[str]) -> "ElementStatus":
        if not reasons:
            return self
        return ElementStatus(state, [*self.reasons, *reasons])


@dataclass
class AddonPackageRecord:
    id: str
    name: str
    version: str
    description: str = ""
    migration_status: str = LEGACY_ADAPTER
    roles: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    kind: str = "toolkit"
    distribution: dict[str, Any] = field(default_factory=dict)
    runtime_units: list[dict[str, Any]] = field(default_factory=list)
    capability_requirements: list[dict[str, Any]] = field(default_factory=list)
    capability_providers: list[dict[str, Any]] = field(default_factory=list)
    ui_entries: list[dict[str, Any]] = field(default_factory=list)
    visibility: str = "user"
    frontend_tier: str = "schema"
    frontend_build: dict[str, Any] = field(default_factory=dict)
    legacy_addon_type: list[str] = field(default_factory=list)
    status: ElementStatus = field(default_factory=ElementStatus)


@dataclass
class ToolRecord:
    id: str
    addon_id: str
    name: str
    legacy_name: str | None = None
    legacy_prefix: str | None = None
    aliases: list[str] = field(default_factory=list)
    function_schema: dict[str, Any] = field(default_factory=dict)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    side_effects: list[str] = field(default_factory=list)
    approval_required: bool = False
    resource_requirements: list[str] = field(default_factory=list)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class ToolViewRecord:
    id: str
    tool_id: str
    name: str = ""
    allowed_parameters: list[str] = field(default_factory=list)
    description: str = ""
    code_callable: bool = False
    batch_policy: str = "serial"
    idempotency: str = "unknown"
    limits: dict[str, Any] = field(default_factory=dict)
    status: ElementStatus = field(default_factory=ElementStatus)


@dataclass
class UiSurfaceRecord:
    id: str
    addon_id: str
    placement: str
    name: str
    view_func: str | None = None
    icon: str = "fa-puzzle-piece"
    legacy_placement: str | None = None
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class HelperRecord:
    id: str
    addon_id: str
    helper_id: str
    name: str
    description: str = ""
    view_func: str | None = None
    icon: str = "fa-puzzle-piece"
    template: str | None = None
    module: str | None = None
    api_version: str | None = None
    css_assets: list[str] = field(default_factory=list)
    legacy_css_assets: list[str] = field(default_factory=list)
    js_assets: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    javascript_api: dict[str, Any] = field(default_factory=dict)
    input_schema_id: str | None = None
    output_schema_id: str | None = None
    options_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
    auth: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    load_on_chat_page: bool = False
    css_on_chat_page: bool = False
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class UiContributionRecord:
    id: str
    addon_id: str
    kind: str
    title: str
    description: str = ""
    component_id: str | None = None
    workflow_id: str | None = None
    surfaces: list[dict[str, Any]] = field(default_factory=list)
    entrypoints: list[dict[str, Any]] = field(default_factory=list)
    workspace: dict[str, Any] = field(default_factory=dict)
    required_addons: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    required_tool_views: list[str] = field(default_factory=list)
    required_helpers: list[str] = field(default_factory=list)
    required_providers: list[str] = field(default_factory=list)
    required_services: list[str] = field(default_factory=list)
    required_workers: list[str] = field(default_factory=list)
    auth_scopes: list[str] = field(default_factory=list)
    resource_scopes: list[str] = field(default_factory=list)
    approval_gates: list[str] = field(default_factory=list)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit.ui_contributions"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderRecord:
    id: str
    addon_id: str
    provider_id: str
    domain: str
    name: str
    kind: str = "local"
    operations: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    runtime: str = "python"
    health: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    service_ids: list[str] = field(default_factory=list)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class ServiceRecord:
    id: str
    addon_id: str
    name: str
    runtime: str = "docker"
    lifecycle: str = "on_demand"
    healthcheck_path: str | None = None
    internal_port: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class WorkerRecord:
    id: str
    addon_id: str
    name: str
    entrypoint: str | None = None
    runtime: str = "python"
    execution_environment: str = "auto"
    allow_environment_fallback: bool = False
    app_runtime_bridge: str = "none"
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class ScheduledJobRecord:
    id: str
    addon_id: str
    name: str
    schedule: dict[str, Any] = field(default_factory=dict)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class PromptRecord:
    id: str
    addon_id: str
    prompt_type: str
    name: str
    content: str = ""
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit"


@dataclass
class GuidanceRecord:
    id: str
    addon_id: str
    title: str
    guidance_type: str = "guidance"
    applies_to: list[str] = field(default_factory=list)
    summary: str = ""
    content: str = ""
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit.guidance"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillRecord:
    id: str
    name: str
    description: str
    addon_id: str | None = None
    folder_name: str | None = None
    instructions: str = ""
    tool_ids: list[str] = field(default_factory=list)
    tool_view_ids: list[str] = field(default_factory=list)
    helper_ids: list[str] = field(default_factory=list)
    provider_ids: list[str] = field(default_factory=list)
    service_ids: list[str] = field(default_factory=list)
    worker_ids: list[str] = field(default_factory=list)
    prompt_ids: list[str] = field(default_factory=list)
    required_addons: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    required_tool_views: list[str] = field(default_factory=list)
    required_helpers: list[str] = field(default_factory=list)
    required_providers: list[str] = field(default_factory=list)
    required_services: list[str] = field(default_factory=list)
    required_workers: list[str] = field(default_factory=list)
    auth_scopes: list[str] = field(default_factory=list)
    resource_scopes: list[str] = field(default_factory=list)
    approval_gates: list[str] = field(default_factory=list)
    source_path: str | None = None
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit.skills"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowRecord:
    id: str
    name: str
    description: str = ""
    addon_id: str | None = None
    required_addons: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    required_tool_views: list[str] = field(default_factory=list)
    required_helpers: list[str] = field(default_factory=list)
    required_providers: list[str] = field(default_factory=list)
    required_services: list[str] = field(default_factory=list)
    required_workers: list[str] = field(default_factory=list)
    auth_scopes: list[str] = field(default_factory=list)
    resource_scopes: list[str] = field(default_factory=list)
    approval_gates: list[str] = field(default_factory=list)
    expected_jobs: list[dict[str, Any]] = field(default_factory=list)
    expected_artifacts: list[dict[str, Any]] = field(default_factory=list)
    state_links: list[dict[str, Any]] = field(default_factory=list)
    log_links: list[dict[str, Any]] = field(default_factory=list)
    resume_links: list[dict[str, Any]] = field(default_factory=list)
    ui_surface_ids: list[str] = field(default_factory=list)
    launch_url: str | None = None
    dry_run_supported: bool = True
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit.workflows"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentProfileRecord:
    id: str
    addon_id: str
    name: str
    description: str = ""
    skill_ids: list[str] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    prompt_ids: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)
    model_class: str | None = None
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "explicit.agent_profiles"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CapabilityRecord:
    id: str
    addon_id: str
    capability_type: str
    name: str
    description: str = ""
    element_ids: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    guidance_ids: list[str] = field(default_factory=list)
    status: ElementStatus = field(default_factory=ElementStatus)
    source: str = "derived"
    group: str = "Toolkits"
    label: str = ""
    selection_mode: str = "direct"
    source_addon_id: str | None = None
    related_capability_ids: list[str] = field(default_factory=list)
    ui_entry_point: dict[str, Any] = field(default_factory=dict)
    launch: dict[str, Any] = field(default_factory=dict)
    schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class AddonElements:
    package: AddonPackageRecord
    tools: list[ToolRecord] = field(default_factory=list)
    tool_views: list[ToolViewRecord] = field(default_factory=list)
    ui_surfaces: list[UiSurfaceRecord] = field(default_factory=list)
    helpers: list[HelperRecord] = field(default_factory=list)
    ui_contributions: list[UiContributionRecord] = field(default_factory=list)
    providers: list[ProviderRecord] = field(default_factory=list)
    services: list[ServiceRecord] = field(default_factory=list)
    workers: list[WorkerRecord] = field(default_factory=list)
    scheduled_jobs: list[ScheduledJobRecord] = field(default_factory=list)
    prompts: list[PromptRecord] = field(default_factory=list)
    guidance: list[GuidanceRecord] = field(default_factory=list)
    skills: list[SkillRecord] = field(default_factory=list)
    workflows: list[WorkflowRecord] = field(default_factory=list)
    agent_profiles: list[AgentProfileRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ManifestCompatibilityParser:
    """Builds transitional element records from explicit and legacy addon fields."""

    UI_PLACEMENT_ALIASES = {
        "automation": "sidebar",
    }

    def parse(
        self,
        manifest: dict[str, Any],
        instance: Any = None,
        addon_dir: Path | None = None,
        is_active: bool = True,
        docker_available: bool | None = True,
    ) -> AddonElements:
        addon_id = manifest.get("id")
        if not addon_id:
            raise ValueError("Manifest is missing required field 'id'.")

        legacy_types = [str(item) for item in _normalize_list(manifest.get("addon_type")) if item]
        migration_status = self._migration_status_from_manifest(manifest)
        frontend_tier = self._frontend_tier_from_manifest(manifest, legacy_types)
        frontend_reasons = validate_frontend_tier(frontend_tier)
        strict_reasons = self._strict_migration_reasons(manifest, migration_status)
        package_status = _status_for_active_state(is_active)
        if frontend_reasons:
            package_status = package_status.with_reasons(MISCONFIGURED, frontend_reasons)
        if strict_reasons:
            package_status = package_status.with_reasons(MISCONFIGURED, strict_reasons)

        package = AddonPackageRecord(
            id=addon_id,
            name=manifest.get("name", addon_id),
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            migration_status=migration_status,
            roles=self._roles_from_manifest(manifest, legacy_types),
            categories=[str(item) for item in _normalize_list(manifest.get("categories")) if item],
            kind=self._kind_from_manifest(manifest),
            distribution=dict(manifest.get("distribution") or {}),
            runtime_units=[
                dict(item)
                for item in ((manifest.get("runtime") or {}).get("units") or [])
                if isinstance(item, dict)
            ],
            capability_requirements=[
                dict(item)
                for item in ((manifest.get("requires") or {}).get("capabilities") or [])
                if isinstance(item, dict)
            ],
            capability_providers=[
                dict(item)
                for item in ((manifest.get("provides") or {}).get("capabilities") or [])
                if isinstance(item, dict)
            ],
            ui_entries=[
                dict(item)
                for item in ((manifest.get("ui") or {}).get("entries") or [])
                if isinstance(item, dict)
            ],
            visibility=manifest.get("visibility", "user"),
            frontend_tier=frontend_tier,
            frontend_build={"declared": frontend_build_declared(manifest)},
            legacy_addon_type=legacy_types,
            status=package_status,
        )
        elements = AddonElements(package=package)
        elements.warnings.extend(frontend_reasons)
        elements.warnings.extend(strict_reasons)

        elements.services.extend(self._parse_services(manifest, addon_id, is_active, docker_available))
        service_ids = [service.id for service in elements.services]
        elements.prompts.extend(self._parse_prompts(manifest, addon_id, is_active))
        elements.guidance.extend(self._parse_guidance(manifest, addon_id, is_active))
        elements.ui_surfaces.extend(self._parse_ui_surfaces(manifest, addon_id, is_active))
        elements.helpers.extend(self._parse_helpers(manifest, addon_id, addon_dir, is_active))
        elements.ui_contributions.extend(
            self._parse_ui_contributions(manifest, addon_id, is_active, elements.ui_surfaces, elements.helpers)
        )
        elements.providers.extend(
            self._parse_providers(manifest, instance, addon_id, service_ids, is_active, docker_available)
        )
        elements.workers.extend(self._parse_workers(manifest, addon_id, addon_dir, is_active))
        elements.scheduled_jobs.extend(self._parse_scheduled_jobs(manifest, instance, addon_id, is_active))
        elements.tools.extend(self._parse_tools(manifest, instance, addon_id, is_active, elements.warnings))
        elements.tool_views.extend(self._parse_tool_views(manifest, addon_id, is_active))
        elements.skills.extend(self._parse_skills(manifest, addon_id, addon_dir, is_active))
        elements.workflows.extend(self._parse_workflows(manifest, addon_id, is_active))
        elements.agent_profiles.extend(self._parse_agent_profiles(manifest, addon_id, is_active))

        return elements

    def discover_personal_skills(self, skills_dir: Path) -> list[SkillRecord]:
        """Discovers user-owned Agent Skills from the approved instance skills path."""
        if not skills_dir.exists() or not skills_dir.is_dir():
            return []

        records = []
        for skill_dir in sorted(skills_dir.iterdir(), key=lambda item: item.name):
            if not skill_dir.is_dir():
                continue
            records.append(
                self._skill_record_from_directory(
                    skill_dir,
                    addon_id=None,
                    is_active=True,
                    source="personal.skills_dir",
                )
            )
        return records

    def _parse_skills(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        addon_dir: Path | None,
        is_active: bool,
    ) -> list[SkillRecord]:
        records = []
        declared_dirs = set()
        for skill in _normalize_record_list(manifest.get("skills")):
            record, skill_dir = self._skill_record_from_manifest(skill, addon_id, addon_dir, is_active)
            records.append(record)
            if skill_dir:
                declared_dirs.add(skill_dir.resolve())

        skills_root = addon_dir / "skills" if addon_dir else None
        if skills_root and skills_root.exists() and skills_root.is_dir():
            for skill_dir in sorted(skills_root.iterdir(), key=lambda item: item.name):
                if not skill_dir.is_dir() or skill_dir.resolve() in declared_dirs:
                    continue
                if not (skill_dir / "SKILL.md").exists():
                    continue
                records.append(
                    self._skill_record_from_directory(
                        skill_dir,
                        addon_id=addon_id,
                        is_active=is_active,
                        source="addon.skills_dir",
                    )
                )
        return records

    def _skill_record_from_manifest(
        self,
        skill: dict[str, Any],
        addon_id: str,
        addon_dir: Path | None,
        is_active: bool,
    ) -> tuple[SkillRecord, Path | None]:
        blocking_reasons = []
        warning_reasons = []
        frontmatter: dict[str, Any] = {}
        body = ""
        skill_dir = None
        skill_file = None
        source_path = None

        raw_path = skill.get("path") or skill.get("folder") or skill.get("directory")
        if raw_path and addon_dir:
            try:
                path = _safe_relative_path(addon_dir, str(raw_path))
                if path.suffix.lower() == ".md":
                    skill_file = path
                    skill_dir = path.parent
                else:
                    skill_dir = path
                    skill_file = skill_dir / "SKILL.md"
                source_path = str(skill_file.relative_to(addon_dir.resolve()))
            except ValueError:
                blocking_reasons.append(f"Skill path escapes addon directory: {raw_path}")

        if skill_file:
            if skill_file.exists() and skill_file.is_file():
                try:
                    frontmatter, body = _parse_skill_markdown(skill_file)
                    warning_reasons.extend(self._skill_frontmatter_warnings(frontmatter))
                except Exception as exc:
                    blocking_reasons.append(f"Could not read SKILL.md: {exc}")
            else:
                blocking_reasons.append(f"Declared skill is missing SKILL.md: {raw_path}")

        folder_name = skill.get("folder_name") or (skill_dir.name if skill_dir else None)
        return (
            self._skill_record_from_data(
                declaration=skill,
                frontmatter=frontmatter,
                body=body,
                addon_id=addon_id,
                folder_name=folder_name,
                source_path=source_path,
                is_active=is_active,
                source="explicit.skills",
                blocking_reasons=blocking_reasons,
                warning_reasons=warning_reasons,
            ),
            skill_dir,
        )

    def _skill_record_from_directory(
        self,
        skill_dir: Path,
        addon_id: str | None,
        is_active: bool,
        source: str,
    ) -> SkillRecord:
        blocking_reasons = []
        warning_reasons = []
        frontmatter: dict[str, Any] = {}
        body = ""
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists() and skill_file.is_file():
            try:
                frontmatter, body = _parse_skill_markdown(skill_file)
                warning_reasons.extend(self._skill_frontmatter_warnings(frontmatter))
            except Exception as exc:
                blocking_reasons.append(f"Could not read SKILL.md: {exc}")
        else:
            blocking_reasons.append("Agent Skill folder is missing SKILL.md.")

        declaration = {"id": skill_dir.name}
        source_path = str(skill_file)
        return self._skill_record_from_data(
            declaration=declaration,
            frontmatter=frontmatter,
            body=body,
            addon_id=addon_id,
            folder_name=skill_dir.name,
            source_path=source_path,
            is_active=is_active,
            source=source,
            blocking_reasons=blocking_reasons,
            warning_reasons=warning_reasons,
        )

    def _skill_record_from_data(
        self,
        declaration: dict[str, Any],
        frontmatter: dict[str, Any],
        body: str,
        addon_id: str | None,
        folder_name: str | None,
        source_path: str | None,
        is_active: bool,
        source: str,
        blocking_reasons: list[str],
        warning_reasons: list[str],
    ) -> SkillRecord:
        sources = [frontmatter, declaration]
        local_id = declaration.get("id") or folder_name or frontmatter.get("name") or "skill"
        stable_id = _qualified_id(addon_id or "personal", local_id, "skill")

        if folder_name and not re.match(r"^[A-Za-z0-9_.-]+$", folder_name):
            blocking_reasons.append(f"Skill folder name is not portable: {folder_name}")

        name = declaration.get("name") or frontmatter.get("name") or ""
        description = declaration.get("description") or frontmatter.get("description") or ""
        instructions = declaration.get("instructions") or declaration.get("content") or body
        if not name:
            blocking_reasons.append("Skill is missing required frontmatter field 'name'.")
            name = stable_id
        if not description:
            blocking_reasons.append("Skill is missing required frontmatter field 'description'.")
        if not instructions:
            warning_reasons.append("Skill has no reusable instruction body.")

        exposed = self._phase2_exposed_ids(sources, addon_id)
        requirements = self._phase2_requirements(sources, addon_id, exposed)
        metadata = copy.deepcopy(frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {})
        metadata.update(copy.deepcopy(declaration.get("metadata") if isinstance(declaration.get("metadata"), dict) else {}))

        return SkillRecord(
            id=stable_id,
            addon_id=addon_id,
            folder_name=folder_name,
            name=name,
            description=description,
            instructions=instructions,
            source_path=source_path,
            status=self._phase2_initial_status(is_active, blocking_reasons, warning_reasons),
            source=source,
            metadata=metadata,
            **exposed,
            **requirements,
        )

    def _skill_frontmatter_warnings(self, frontmatter: dict[str, Any]) -> list[str]:
        warnings = []
        for key in sorted(frontmatter):
            if key not in PORTABLE_SKILL_FRONTMATTER_KEYS:
                warnings.append(f"Skill frontmatter field is not portable: {key}")
        return warnings

    def _parse_workflows(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[WorkflowRecord]:
        records = []
        for workflow in _normalize_record_list(manifest.get("workflows")):
            workflow_id = _qualified_id(addon_id, workflow.get("id") or workflow.get("name"), "workflow")
            requirements = self._phase2_requirements([workflow], addon_id)
            records.append(
                WorkflowRecord(
                    id=workflow_id,
                    addon_id=addon_id,
                    name=workflow.get("name", workflow_id),
                    description=workflow.get("description", ""),
                    expected_jobs=_normalize_named_record_list(workflow.get("expected_jobs")),
                    expected_artifacts=_normalize_named_record_list(workflow.get("expected_artifacts")),
                    state_links=_normalize_named_record_list(workflow.get("state_links")),
                    log_links=_normalize_named_record_list(workflow.get("log_links")),
                    resume_links=_normalize_named_record_list(workflow.get("resume_links")),
                    ui_surface_ids=self._scoped_ids(addon_id, workflow.get("ui_surfaces") or workflow.get("ui_surface_ids"), True),
                    launch_url=workflow.get("launch_url"),
                    dry_run_supported=bool(workflow.get("dry_run_supported", True)),
                    status=_status_for_active_state(is_active),
                    source="explicit.workflows",
                    metadata=copy.deepcopy(workflow.get("metadata") if isinstance(workflow.get("metadata"), dict) else {}),
                    **requirements,
                )
            )
        return records

    def _parse_agent_profiles(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[AgentProfileRecord]:
        records = []
        for profile in _normalize_record_list(manifest.get("agent_profiles")):
            profile_id = _qualified_id(addon_id, profile.get("id") or profile.get("name"), "agent_profile")
            records.append(
                AgentProfileRecord(
                    id=profile_id,
                    addon_id=addon_id,
                    name=profile.get("name", profile_id),
                    description=profile.get("description", ""),
                    skill_ids=self._scoped_ids(addon_id, profile.get("skills") or profile.get("skill_ids"), True),
                    workflow_ids=self._scoped_ids(addon_id, profile.get("workflows") or profile.get("workflow_ids"), True),
                    prompt_ids=self._scoped_ids(addon_id, profile.get("prompts") or profile.get("prompt_ids"), True),
                    tool_ids=self._scoped_ids(addon_id, profile.get("tools") or profile.get("tool_ids"), True),
                    model_class=profile.get("model_class"),
                    status=_status_for_active_state(is_active),
                    source="explicit.agent_profiles",
                    metadata=copy.deepcopy(profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}),
                )
            )
        return records

    def _phase2_exposed_ids(self, sources: list[dict[str, Any]], addon_id: str | None) -> dict[str, list[str]]:
        return {
            "tool_ids": self._scoped_ids(addon_id, self._collect_phase2_values(sources, "tools", "tool_ids"), True),
            "tool_view_ids": self._scoped_ids(addon_id, self._collect_phase2_values(sources, "tool_views", "tool_view_ids"), True),
            "helper_ids": self._scoped_ids(
                addon_id,
                self._collect_phase2_values(sources, "ui_components", "ui_component_ids", "helpers", "helper_ids"),
                True,
            ),
            "provider_ids": self._collect_phase2_values(sources, "providers", "provider_ids"),
            "service_ids": self._scoped_ids(addon_id, self._collect_phase2_values(sources, "services", "service_ids"), True),
            "worker_ids": self._scoped_ids(addon_id, self._collect_phase2_values(sources, "workers", "worker_ids"), True),
            "prompt_ids": self._scoped_ids(addon_id, self._collect_phase2_values(sources, "prompts", "prompt_ids"), True),
        }

    def _phase2_requirements(
        self,
        sources: list[dict[str, Any]],
        addon_id: str | None,
        exposed: dict[str, list[str]] | None = None,
    ) -> dict[str, list[str]]:
        exposed = exposed or {}
        return {
            "required_addons": self._collect_phase2_values(sources, "addons", "required_addons"),
            "required_tools": self._scoped_ids(
                addon_id,
                _merge_unique(
                    self._collect_phase2_values(sources, "tools", "tool_ids", "required_tools"),
                    exposed.get("tool_ids", []),
                ),
                True,
            ),
            "required_tool_views": self._scoped_ids(
                addon_id,
                _merge_unique(
                    self._collect_phase2_values(sources, "tool_views", "tool_view_ids", "required_tool_views"),
                    exposed.get("tool_view_ids", []),
                ),
                True,
            ),
            "required_helpers": self._scoped_ids(
                addon_id,
                _merge_unique(
                    self._collect_phase2_values(
                        sources,
                        "ui_components",
                        "ui_component_ids",
                        "required_ui_components",
                        "helpers",
                        "helper_ids",
                        "required_helpers",
                    ),
                    exposed.get("helper_ids", []),
                ),
                True,
            ),
            "required_providers": _merge_unique(
                self._collect_phase2_values(sources, "providers", "provider_ids", "required_providers"),
                exposed.get("provider_ids", []),
            ),
            "required_services": self._scoped_ids(
                addon_id,
                _merge_unique(
                    self._collect_phase2_values(sources, "services", "service_ids", "required_services"),
                    exposed.get("service_ids", []),
                ),
                True,
            ),
            "required_workers": self._scoped_ids(
                addon_id,
                _merge_unique(
                    self._collect_phase2_values(sources, "workers", "worker_ids", "required_workers"),
                    exposed.get("worker_ids", []),
                ),
                True,
            ),
            "auth_scopes": self._collect_phase2_values(sources, "auth_scopes"),
            "resource_scopes": self._collect_phase2_values(sources, "resource_scopes"),
            "approval_gates": self._collect_phase2_values(sources, "approval_gates"),
        }

    def _collect_phase2_values(self, sources: list[dict[str, Any]], *keys: str) -> list[str]:
        values = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            requirements = source.get("requirements") if isinstance(source.get("requirements"), dict) else {}
            requires = source.get("requires") if isinstance(source.get("requires"), dict) else {}
            for key in keys:
                values.extend(_normalize_string_list(source.get(key)))
                values.extend(_normalize_string_list(requirements.get(key)))
                values.extend(_normalize_string_list(requires.get(key)))
        return _merge_unique(values)

    def _scoped_ids(self, addon_id: str | None, values: Any, qualify_local: bool) -> list[str]:
        scoped = []
        for value in _normalize_string_list(values):
            if qualify_local and addon_id and "." not in value and not value.startswith(f"{addon_id}_"):
                value = f"{addon_id}.{value}"
            scoped.append(value)
        return _merge_unique(scoped)

    def _phase2_initial_status(
        self,
        is_active: bool,
        blocking_reasons: list[str],
        warning_reasons: list[str],
    ) -> ElementStatus:
        if not is_active:
            return _status_for_active_state(False)
        if blocking_reasons:
            return ElementStatus(MISCONFIGURED, blocking_reasons)
        if warning_reasons:
            return ElementStatus(DEGRADED, warning_reasons)
        return ElementStatus(ACTIVE)

    def _roles_from_manifest(self, manifest: dict[str, Any], legacy_types: list[str]) -> list[str]:
        roles = [str(item) for item in _normalize_list(manifest.get("roles")) if item]
        if roles:
            return roles
        if manifest.get("manifest_schema_version") == 2:
            kind = self._kind_from_manifest(manifest)
            return {
                "application": ["workspace"],
                "toolkit": ["toolkit"],
                "connector": ["integration", "provider"],
                "engine": ["service", "provider"],
                "component": ["ui_component"],
            }.get(kind, ["package"])

        mapped = []
        for legacy_type in legacy_types:
            if legacy_type == "plugin":
                mapped.append("toolkit")
            elif legacy_type == "automation":
                mapped.append("workspace")
            elif legacy_type == "helper":
                mapped.append("ui_component")
            elif legacy_type == "page":
                mapped.append("page")
            elif legacy_type == "system":
                mapped.append("provider")
            else:
                mapped.append(legacy_type)
        return sorted(set(mapped)) or ["package"]

    def _migration_status_from_manifest(self, manifest: dict[str, Any]) -> str:
        raw_status = manifest.get("migration_status") or manifest.get("refactor_status") or LEGACY_ADAPTER
        status = str(raw_status).strip() if raw_status is not None else LEGACY_ADAPTER
        return status if status in VALID_MIGRATION_STATUSES else LEGACY_ADAPTER

    def _strict_migration_reasons(self, manifest: dict[str, Any], migration_status: str) -> list[str]:
        if migration_status != EXPLICIT_MIGRATED:
            return []

        reasons = []
        for field_name in sorted(STRICT_EXPLICIT_LEGACY_FIELDS):
            if field_name in manifest:
                reasons.append(f"Explicit-migrated addons must not declare legacy field '{field_name}'.")

        dependencies = manifest.get("dependencies") or {}
        if isinstance(dependencies, dict):
            if dependencies.get("plugins"):
                reasons.append("Explicit-migrated addons must use dependencies.addons instead of dependencies.plugins.")
            if dependencies.get("helpers"):
                reasons.append("Explicit-migrated addons must use dependencies.ui_components instead of dependencies.helpers.")

        if "manifest_schema_version" not in manifest:
            reasons.append("Explicit-migrated addons must declare manifest_schema_version.")
        if manifest.get("manifest_schema_version") == 2:
            for field_name in (
                "kind",
                "distribution",
                "runtime",
                "ui",
                "provides",
                "requires",
            ):
                if field_name not in manifest:
                    reasons.append(
                        f"Manifest schema 2 addons must declare '{field_name}'."
                    )
        else:
            if not manifest.get("roles"):
                reasons.append("Explicit-migrated addons must declare explicit roles.")
            if not manifest.get("categories"):
                reasons.append("Explicit-migrated addons must declare explicit categories.")
            frontend = manifest.get("frontend") or {}
            if not isinstance(frontend, dict) or not frontend.get("tier"):
                reasons.append("Explicit-migrated addons must declare frontend.tier.")

        return reasons

    def _frontend_tier_from_manifest(self, manifest: dict[str, Any], legacy_types: list[str]) -> str:
        if manifest.get("manifest_schema_version") == 2:
            entries = (manifest.get("ui") or {}).get("entries") or []
            modes = {
                str(item.get("mode"))
                for item in entries
                if isinstance(item, dict) and item.get("mode")
            }
            if "svelte_application" in modes:
                return "compiled_workspace"
            if "svelte_component" in modes and len(modes) > 1:
                return "hybrid"
            if "external" in modes:
                return "external_service"
            return "schema"
        frontend = manifest.get("frontend") or {}
        if isinstance(frontend, dict) and frontend.get("tier"):
            return frontend["tier"]
        if any(item in legacy_types for item in ("automation", "page")):
            return "server_workspace"
        if "helper" in legacy_types or manifest.get("ui_entry_points"):
            return "jinja"
        return "schema"

    @staticmethod
    def _kind_from_manifest(manifest: dict[str, Any]) -> str:
        explicit = str(manifest.get("kind") or "").strip()
        if explicit:
            return explicit
        roles = {str(item) for item in _normalize_list(manifest.get("roles"))}
        if "service" in roles:
            return "engine"
        if roles.intersection({"workspace", "page"}):
            return "application"
        if "provider" in roles and "integration" in roles and "toolkit" not in roles:
            return "connector"
        if "ui_component" in roles and not roles.intersection(
            {"toolkit", "provider", "workspace", "page"}
        ):
            return "component"
        return "toolkit"

    def _parse_prompts(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[PromptRecord]:
        records = []
        status = _status_for_active_state(is_active)
        if manifest.get("plugin_prompt"):
            records.append(
                PromptRecord(
                    id=f"{addon_id}.plugin_prompt",
                    addon_id=addon_id,
                    prompt_type="plugin",
                    name="Plugin Prompt",
                    content=manifest.get("plugin_prompt", ""),
                    status=status,
                    source="legacy.plugin_prompt",
                )
            )

        automation_prompts = manifest.get("automation_prompts") or {}
        if isinstance(automation_prompts, dict):
            for key, content in automation_prompts.items():
                records.append(
                    PromptRecord(
                        id=f"{addon_id}.automation.{_safe_id(key)}",
                        addon_id=addon_id,
                        prompt_type="automation",
                        name=str(key),
                        content=str(content or ""),
                        status=status,
                        source="legacy.automation_prompts",
                    )
                )

        for prompt in _normalize_record_list(manifest.get("prompts")):
            if prompt.get("enabled") is False:
                continue
            prompt_id = _qualified_id(addon_id, prompt.get("id") or prompt.get("name"), "prompt")
            records.append(
                PromptRecord(
                    id=prompt_id,
                    addon_id=addon_id,
                    prompt_type=prompt.get("type", "prompt"),
                    name=prompt.get("name", prompt_id),
                    content=prompt.get("content", ""),
                    status=status,
                    source="explicit.prompts",
                )
            )
        return records

    def _parse_guidance(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[GuidanceRecord]:
        records = []
        status = _status_for_active_state(is_active)
        for guidance in _normalize_record_list(manifest.get("guidance")):
            guidance_id = _qualified_id(addon_id, guidance.get("id") or guidance.get("title"), "guidance")
            applies_to = _merge_unique(
                self._scoped_ids(
                    addon_id,
                    guidance.get("applies_to") or guidance.get("capability_ids") or guidance.get("capabilities"),
                    True,
                )
            )
            records.append(
                GuidanceRecord(
                    id=guidance_id,
                    addon_id=addon_id,
                    title=guidance.get("title", guidance_id),
                    guidance_type=guidance.get("type", guidance.get("guidance_type", "guidance")),
                    applies_to=applies_to,
                    summary=guidance.get("summary", ""),
                    content=guidance.get("content", ""),
                    status=status,
                    source="explicit.guidance",
                    metadata=copy.deepcopy(guidance.get("metadata") if isinstance(guidance.get("metadata"), dict) else {}),
                )
            )
        return records

    def _parse_ui_surfaces(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[UiSurfaceRecord]:
        records = []
        status = _status_for_active_state(is_active)
        entry_points = manifest.get("ui_entry_points") or {}
        if isinstance(entry_points, dict):
            for raw_placement, entry in entry_points.items():
                if not isinstance(entry, dict):
                    continue
                placement = self.UI_PLACEMENT_ALIASES.get(raw_placement, raw_placement)
                ui_id = _qualified_id(addon_id, entry.get("id") or placement, placement)
                records.append(
                    UiSurfaceRecord(
                        id=ui_id,
                        addon_id=addon_id,
                        placement=placement,
                        name=entry.get("name", manifest.get("name", addon_id)),
                        view_func=entry.get("view_func"),
                        icon=entry.get("icon", "fa-puzzle-piece"),
                        legacy_placement=raw_placement if raw_placement != placement else None,
                        status=status,
                        source="manifest.ui_entry_points",
                    )
                )

        for surface in _normalize_record_list(manifest.get("ui_surfaces")):
            placement = surface.get("placement") or surface.get("type") or "page"
            records.append(
                UiSurfaceRecord(
                    id=_qualified_id(addon_id, surface.get("id") or placement, placement),
                    addon_id=addon_id,
                    placement=placement,
                    name=surface.get("name", manifest.get("name", addon_id)),
                    view_func=surface.get("view_func"),
                    icon=surface.get("icon", "fa-puzzle-piece"),
                    status=status,
                    source="explicit.ui_surfaces",
                )
            )
        return records

    def _parse_helpers(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        addon_dir: Path | None,
        is_active: bool,
    ) -> list[HelperRecord]:
        records = []
        ui_helper = (manifest.get("ui_entry_points") or {}).get("helper") or {}
        explicit_components = _normalize_record_list(manifest.get("ui_components"))
        explicit_helpers = _normalize_record_list(manifest.get("helpers"))
        integration_reference = manifest.get("integration_reference") or {}
        javascript_api = integration_reference.get("javascript_api") or {}

        for component in explicit_components:
            records.append(
                self._helper_record_from_manifest(
                    component,
                    manifest,
                    addon_id,
                    addon_dir,
                    is_active,
                    ui_helper,
                    source="explicit.ui_components",
                )
            )

        for helper in explicit_helpers:
            records.append(self._helper_record_from_manifest(helper, manifest, addon_id, addon_dir, is_active, ui_helper))

        legacy_types = _normalize_list(manifest.get("addon_type"))
        if not records and ("helper" in legacy_types or ui_helper):
            helper = {
                "id": addon_id,
                "name": ui_helper.get("name", manifest.get("name", addon_id)),
                "description": ui_helper.get("description", manifest.get("description", "")),
                "view_func": ui_helper.get("view_func"),
                "icon": ui_helper.get("icon", "fa-puzzle-piece"),
                "load_on_chat_page": ui_helper.get("load_on_chat_page", False),
                "css_on_chat_page": ui_helper.get("css_on_chat_page", False),
                "javascript_api": javascript_api,
                "result_schema": javascript_api.get("output_schema", {}),
                "aliases": [addon_id, f"{addon_id}.default"],
            }
            records.append(self._helper_record_from_manifest(helper, manifest, addon_id, addon_dir, is_active, ui_helper, source="legacy.helper"))
        return records

    def _helper_record_from_manifest(
        self,
        helper: dict[str, Any],
        manifest: dict[str, Any],
        addon_id: str,
        addon_dir: Path | None,
        is_active: bool,
        ui_helper: dict[str, Any],
        source: str = "explicit.helpers",
    ) -> HelperRecord:
        helper_local_id = helper.get("id") or helper.get("helper_id") or addon_id
        stable_id = _qualified_id(addon_id, helper_local_id, "helper")
        css_assets = [str(item) for item in _normalize_list(helper.get("css_assets")) if item]
        legacy_css_assets = [
            str(item)
            for item in _normalize_list(helper.get("legacy_css_assets"))
            if item
        ]
        js_assets = [str(item) for item in _normalize_list(helper.get("js_assets")) if item]
        template = helper.get("template")
        module = helper.get("module") or helper.get("module_url")
        api_version = helper.get("api_version") or helper.get("frontend_api_version")
        options_schema = helper.get("options_schema", {})
        result_schema = helper.get("result_schema", {})
        input_schema_id = helper.get("input_schema_id")
        output_schema_id = helper.get("output_schema_id")
        if options_schema and not input_schema_id:
            input_schema_id = f"{stable_id}.input"
        if result_schema and not output_schema_id:
            output_schema_id = f"{stable_id}.output"

        if not template and addon_dir and (addon_dir / "templates" / f"{addon_id}.html").exists():
            template = f"templates/{addon_id}.html"
        if not css_assets and (helper.get("css_on_chat_page") or ui_helper.get("css_on_chat_page")):
            css_assets = [f"static/{addon_id}.css"]
        if not js_assets and (helper.get("load_on_chat_page") or ui_helper.get("load_on_chat_page")):
            js_assets = [f"static/{addon_id}.js"]

        declared_assets = [
            *css_assets,
            *legacy_css_assets,
            *js_assets,
            *([str(module)] if module else []),
            *([template] if template else []),
        ]
        missing_assets = _missing_asset_reasons(addon_dir, declared_assets)
        status = _status_for_active_state(is_active)
        if is_active and missing_assets:
            status = status.with_reasons(DEGRADED, missing_assets)

        aliases = [stable_id, addon_id, *_normalize_list(helper.get("aliases"))]
        if "." not in str(helper_local_id):
            aliases.append(f"{addon_id}.{helper_local_id}")

        return HelperRecord(
            id=stable_id,
            addon_id=addon_id,
            helper_id=str(helper_local_id),
            name=helper.get("name", ui_helper.get("name", manifest.get("name", addon_id))),
            description=helper.get("description", ui_helper.get("description", manifest.get("description", ""))),
            view_func=helper.get("view_func", ui_helper.get("view_func")),
            icon=helper.get("icon", ui_helper.get("icon", "fa-puzzle-piece")),
            template=template,
            module=str(module) if module else None,
            api_version=str(api_version) if api_version else None,
            css_assets=css_assets,
            legacy_css_assets=legacy_css_assets,
            js_assets=js_assets,
            aliases=sorted({str(alias) for alias in aliases if alias}),
            javascript_api=helper.get("javascript_api", {}),
            input_schema_id=str(input_schema_id) if input_schema_id else None,
            output_schema_id=str(output_schema_id) if output_schema_id else None,
            options_schema=options_schema,
            result_schema=result_schema,
            auth=[str(item) for item in _normalize_list(helper.get("auth")) if item],
            resources=[str(item) for item in _normalize_list(helper.get("resources")) if item],
            load_on_chat_page=bool(helper.get("load_on_chat_page", ui_helper.get("load_on_chat_page", False))),
            css_on_chat_page=bool(helper.get("css_on_chat_page", ui_helper.get("css_on_chat_page", False))),
            status=status,
            source=source,
        )

    def _parse_ui_contributions(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        is_active: bool,
        ui_surfaces: list[UiSurfaceRecord],
        helpers: list[HelperRecord],
    ) -> list[UiContributionRecord]:
        records = []
        referenced_components = set()

        for contribution in _normalize_record_list(manifest.get("ui_contributions")):
            record = self._ui_contribution_record_from_manifest(contribution, addon_id, is_active)
            if record.component_id:
                referenced_components.add(record.component_id)
            records.append(record)

        if records:
            return records

        for surface in ui_surfaces:
            if surface.placement not in {"sidebar", "page"}:
                continue
            records.append(self._ui_contribution_from_legacy_surface(surface, manifest, is_active))

        for helper in helpers:
            if helper.id in referenced_components:
                continue
            records.append(self._ui_contribution_from_helper(helper, is_active))

        return records

    def _ui_contribution_record_from_manifest(
        self,
        contribution: dict[str, Any],
        addon_id: str,
        is_active: bool,
    ) -> UiContributionRecord:
        contribution_id = _qualified_id(addon_id, contribution.get("id") or contribution.get("name") or contribution.get("title"), "ui")
        component_id = self._optional_scoped_id(addon_id, contribution.get("component_id") or contribution.get("component") or contribution.get("ui_component"))
        workflow_id = self._optional_scoped_id(addon_id, contribution.get("workflow_id") or contribution.get("workflow"))
        requirements = self._phase2_requirements([contribution], addon_id)
        if component_id:
            requirements["required_helpers"] = _merge_unique(requirements["required_helpers"], [component_id])

        return UiContributionRecord(
            id=contribution_id,
            addon_id=addon_id,
            kind=str(contribution.get("kind") or contribution.get("type") or "workflow"),
            title=contribution.get("title") or contribution.get("name") or contribution_id,
            description=contribution.get("description", ""),
            component_id=component_id,
            workflow_id=workflow_id,
            surfaces=self._normalize_ui_specs(contribution.get("surfaces") or contribution.get("surface")),
            entrypoints=self._normalize_ui_specs(contribution.get("entrypoints") or contribution.get("entry_points") or contribution.get("entrypoint")),
            workspace=copy.deepcopy(contribution.get("workspace") if isinstance(contribution.get("workspace"), dict) else {}),
            status=_status_for_active_state(is_active),
            source="explicit.ui_contributions",
            metadata=copy.deepcopy(contribution.get("metadata") if isinstance(contribution.get("metadata"), dict) else {}),
            **requirements,
        )

    def _ui_contribution_from_legacy_surface(
        self,
        surface: UiSurfaceRecord,
        manifest: dict[str, Any],
        is_active: bool,
    ) -> UiContributionRecord:
        is_workspace = surface.placement == "sidebar"
        kind = "workspace" if is_workspace else "task_page"
        entrypoint_kind = "workspace_launcher" if is_workspace else "tools_tab"
        section = "Workspaces" if is_workspace else "Tools"
        source = "legacy.ui_entry_points" if surface.source == "manifest.ui_entry_points" else "legacy.ui_surfaces"

        return UiContributionRecord(
            id=f"{surface.id}.ui",
            addon_id=surface.addon_id,
            kind=kind,
            title=surface.name,
            description=manifest.get("description", ""),
            surfaces=[
                {
                    "kind": "page",
                    "view_func": surface.view_func,
                    "legacy_placement": surface.legacy_placement or surface.placement,
                }
            ],
            entrypoints=[
                {
                    "kind": entrypoint_kind,
                    "section": section,
                    "icon": surface.icon,
                }
            ],
            status=_status_for_active_state(is_active),
            source=source,
        )

    def _ui_contribution_from_helper(self, helper: HelperRecord, is_active: bool) -> UiContributionRecord:
        kind = "picker" if helper.result_schema else "quick_action"
        entrypoints = []
        if helper.load_on_chat_page or helper.css_on_chat_page:
            entrypoints.append({"kind": "quick_access", "section": "Chat", "icon": helper.icon})
        else:
            entrypoints.append({"kind": "command_palette", "section": "Components", "icon": helper.icon})

        return UiContributionRecord(
            id=f"{helper.id}.ui",
            addon_id=helper.addon_id,
            kind=kind,
            title=helper.name,
            description=helper.description,
            component_id=helper.id,
            surfaces=[{"kind": "modal", "view_func": helper.view_func}],
            entrypoints=entrypoints,
            status=_status_for_active_state(is_active),
            source="derived.ui_component" if helper.source == "explicit.ui_components" else "legacy.helper",
        )

    def _optional_scoped_id(self, addon_id: str, value: Any) -> str | None:
        values = self._scoped_ids(addon_id, [value], True) if value else []
        return values[0] if values else None

    def _normalize_ui_specs(self, value: Any) -> list[dict[str, Any]]:
        specs = []
        for item in _normalize_record_list(value):
            spec = copy.deepcopy(item)
            kind = spec.get("kind") or spec.get("type") or spec.get("placement") or spec.get("id")
            if kind:
                spec["kind"] = str(kind)
            specs.append(spec)
        return specs

    def _parse_providers(
        self,
        manifest: dict[str, Any],
        instance: Any,
        addon_id: str,
        service_ids: list[str],
        is_active: bool,
        docker_available: bool | None,
    ) -> list[ProviderRecord]:
        records = []
        records.extend(self._parse_domain_providers(manifest, addon_id, "audio", service_ids, is_active, docker_available))
        records.extend(self._parse_domain_providers(manifest, addon_id, "scraping", service_ids, is_active, docker_available))
        records.extend(self._parse_generic_providers(manifest, addon_id, service_ids, is_active, docker_available))

        existing_ids = {record.id for record in records}
        if instance is not None:
            scrape = self._legacy_scrape_provider(instance, addon_id, service_ids, is_active, docker_available)
            if scrape and scrape.id not in existing_ids:
                records.append(scrape)
                existing_ids.add(scrape.id)

            extractor = self._legacy_document_extractor(instance, addon_id, service_ids, is_active, docker_available)
            if extractor and extractor.id not in existing_ids:
                records.append(extractor)
        return records

    def _parse_domain_providers(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        domain: str,
        service_ids: list[str],
        is_active: bool,
        docker_available: bool | None,
    ) -> list[ProviderRecord]:
        section = manifest.get(domain) or {}
        if not isinstance(section, dict):
            return []

        records = []
        for provider in _normalize_record_list(section.get("providers")):
            provider_id = str(provider.get("id") or addon_id)
            runtime = str(provider.get("runtime") or ("docker" if manifest.get("docker") else "python"))
            model_operations = []
            for model in provider.get("models") or []:
                if isinstance(model, dict) and model.get("operation"):
                    model_operations.append(model["operation"])
            capabilities = [str(item) for item in _normalize_list(provider.get("capabilities")) if item]
            operations = [str(item) for item in _normalize_list(provider.get("operations")) if item]
            if not operations:
                operations = model_operations or capabilities
            records.append(
                ProviderRecord(
                    id=f"{domain}.{provider_id}",
                    addon_id=addon_id,
                    provider_id=provider_id,
                    domain=domain,
                    name=provider.get("name", provider_id),
                    kind=provider.get("kind", "local" if manifest.get("docker") else "cloud"),
                    operations=sorted(set(operations)),
                    capabilities=capabilities,
                    runtime=runtime,
                    health=self._provider_health(provider, manifest),
                    config=copy.deepcopy(provider),
                    aliases=sorted({provider_id, f"{addon_id}.{provider_id}", f"{domain}.{provider_id}"}),
                    service_ids=service_ids,
                    status=self._provider_status(manifest, is_active, docker_available, runtime=runtime),
                    source=f"manifest.{domain}.providers",
                )
            )
        return records

    def _parse_generic_providers(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        service_ids: list[str],
        is_active: bool,
        docker_available: bool | None,
    ) -> list[ProviderRecord]:
        records = []
        for provider in _normalize_record_list(manifest.get("providers")):
            domain = provider.get("domain", "generic")
            provider_id = str(provider.get("id") or addon_id)
            runtime = str(provider.get("runtime") or ("docker" if manifest.get("docker") else "python"))
            records.append(
                ProviderRecord(
                    id=f"{domain}.{provider_id}",
                    addon_id=addon_id,
                    provider_id=provider_id,
                    domain=domain,
                    name=provider.get("name", provider_id),
                    kind=provider.get("kind", "local" if manifest.get("docker") else "cloud"),
                    operations=[str(item) for item in _normalize_list(provider.get("operations")) if item],
                    capabilities=[str(item) for item in _normalize_list(provider.get("capabilities")) if item],
                    runtime=runtime,
                    health=self._provider_health(provider, manifest),
                    config=copy.deepcopy(provider),
                    aliases=sorted({provider_id, f"{addon_id}.{provider_id}", f"{domain}.{provider_id}"}),
                    service_ids=service_ids,
                    status=self._provider_status(manifest, is_active, docker_available, runtime=runtime),
                    source="manifest.providers",
                )
            )
        return records

    def _legacy_scrape_provider(
        self,
        instance: Any,
        addon_id: str,
        service_ids: list[str],
        is_active: bool,
        docker_available: bool | None,
    ) -> ProviderRecord | None:
        if not getattr(instance, "is_scrape_provider", False):
            return None
        provider_id = getattr(instance, "scrape_provider_id", addon_id)
        manifest = getattr(instance, "manifest", {}) or {}
        return ProviderRecord(
            id=f"scraping.{provider_id}",
            addon_id=addon_id,
            provider_id=provider_id,
            domain="scraping",
            name=getattr(instance, "scrape_provider_name", provider_id),
            kind=getattr(instance, "scrape_provider_kind", "local"),
            operations=[str(item) for item in _normalize_list(getattr(instance, "scrape_provider_operations", [])) if item],
            capabilities=[str(item) for item in _normalize_list(getattr(instance, "scrape_provider_capabilities", [])) if item],
            runtime="docker" if manifest.get("docker") else "python",
            aliases=sorted({provider_id, f"{addon_id}.{provider_id}", f"scraping.{provider_id}"}),
            service_ids=service_ids,
            status=self._provider_status(
                manifest,
                is_active,
                docker_available,
                runtime="docker" if manifest.get("docker") else "python",
            ),
            source="legacy.class_attributes.scrape_provider",
        )

    def _legacy_document_extractor(
        self,
        instance: Any,
        addon_id: str,
        service_ids: list[str],
        is_active: bool,
        docker_available: bool | None,
    ) -> ProviderRecord | None:
        if not getattr(instance, "is_doc_extractor", False):
            return None
        provider_id = getattr(instance, "extractor_id", addon_id)
        manifest = getattr(instance, "manifest", {}) or {}
        return ProviderRecord(
            id=f"document.{provider_id}",
            addon_id=addon_id,
            provider_id=provider_id,
            domain="document",
            name=getattr(instance, "extractor_name", provider_id),
            kind="local",
            operations=["document.extract"],
            capabilities=[str(item) for item in _normalize_list(getattr(instance, "extractor_capabilities", [])) if item],
            runtime="docker" if manifest.get("docker") else "python",
            aliases=sorted({provider_id, f"{addon_id}.{provider_id}", f"document.{provider_id}"}),
            service_ids=service_ids,
            status=self._provider_status(
                manifest,
                is_active,
                docker_available,
                runtime="docker" if manifest.get("docker") else "python",
            ),
            source="legacy.class_attributes.doc_extractor",
        )

    def _provider_health(self, provider: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        health = copy.deepcopy(provider.get("health") or {})
        docker_config = manifest.get("docker") or {}
        services = docker_config.get("services") or {}
        primary = next((service for service in services.values() if isinstance(service, dict) and service.get("primary")), None)
        if not primary and isinstance(services.get("service"), dict):
            primary = services["service"]
        if primary and primary.get("healthcheck_path"):
            health.setdefault("path", primary.get("healthcheck_path"))
        return health

    def _provider_status(
        self,
        manifest: dict[str, Any],
        is_active: bool,
        docker_available: bool | None,
        runtime: str | None = None,
    ) -> ElementStatus:
        status = _status_for_active_state(is_active)
        provider_runtime = str(runtime or ("docker" if manifest.get("docker") else "python"))
        if (
            is_active
            and provider_runtime == "docker"
            and manifest.get("docker")
            and docker_available is False
        ):
            return status.with_reasons(DEGRADED, ["Docker-backed provider is unavailable because Docker is not available."])
        return status

    def _parse_services(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        is_active: bool,
        docker_available: bool | None,
    ) -> list[ServiceRecord]:
        docker_config = manifest.get("docker") or {}
        if not isinstance(docker_config, dict) or not docker_config:
            return []
        services = docker_config.get("services") or {"service": docker_config}
        lifecycle = docker_config.get("lifecycle", "on_demand")
        records = []
        for name, service in services.items():
            if not isinstance(service, dict):
                continue
            status = _status_for_active_state(is_active)
            if is_active and docker_available is False:
                status = status.with_reasons(DEGRADED, ["Docker Engine is not available."])
            records.append(
                ServiceRecord(
                    id=f"{addon_id}.{name}",
                    addon_id=addon_id,
                    name=str(name),
                    runtime="docker",
                    lifecycle=service.get("lifecycle", lifecycle),
                    healthcheck_path=service.get("healthcheck_path"),
                    internal_port=service.get("internal_port"),
                    config=copy.deepcopy(service),
                    status=status,
                    source="manifest.docker",
                )
            )
        return records

    def _parse_workers(
        self,
        manifest: dict[str, Any],
        addon_id: str,
        addon_dir: Path | None,
        is_active: bool,
    ) -> list[WorkerRecord]:
        records = []
        for worker in _normalize_record_list(manifest.get("workers")):
            worker_id = _qualified_id(addon_id, worker.get("id") or worker.get("name"), "worker")
            execution_environment = str(
                worker.get("execution_environment")
                or worker.get("worker_environment")
                or "auto"
            ).strip().lower() or "auto"
            allow_environment_fallback = worker.get("allow_environment_fallback")
            if allow_environment_fallback is None:
                allow_environment_fallback = worker.get("allow_fallback")
            if allow_environment_fallback is None:
                allow_environment_fallback = False
            app_runtime_bridge = str(
                worker.get("app_runtime_bridge") or "none"
            ).strip().lower()
            records.append(
                WorkerRecord(
                    id=worker_id,
                    addon_id=addon_id,
                    name=worker.get("name", worker_id),
                    entrypoint=worker.get("entrypoint"),
                    runtime=worker.get("runtime", "python"),
                    execution_environment=execution_environment,
                    allow_environment_fallback=bool(allow_environment_fallback),
                    app_runtime_bridge=app_runtime_bridge,
                    status=_status_for_active_state(is_active),
                    source="explicit.workers",
                )
            )

        if addon_dir and (addon_dir / "worker.py").exists() and not records:
            records.append(
                WorkerRecord(
                    id=f"{addon_id}.worker",
                    addon_id=addon_id,
                    name="Legacy Worker",
                    entrypoint="worker.py",
                    execution_environment="auto",
                    allow_environment_fallback=False,
                    status=_status_for_active_state(is_active),
                    source="legacy.worker_file",
                )
            )
        return records

    def _parse_scheduled_jobs(
        self,
        manifest: dict[str, Any],
        instance: Any,
        addon_id: str,
        is_active: bool,
    ) -> list[ScheduledJobRecord]:
        records = []
        for job in _normalize_record_list(manifest.get("scheduled_jobs")):
            job_id = _qualified_id(addon_id, job.get("id") or job.get("name"), "job")
            records.append(
                ScheduledJobRecord(
                    id=job_id,
                    addon_id=addon_id,
                    name=job.get("name", job_id),
                    schedule=copy.deepcopy(job.get("schedule") or {}),
                    status=_status_for_active_state(is_active),
                    source="explicit.scheduled_jobs",
                )
            )

        if instance is not None and self._overrides_addon_base_method(instance, "on_register_jobs") and not records:
            records.append(
                ScheduledJobRecord(
                    id=f"{addon_id}.legacy_on_register_jobs",
                    addon_id=addon_id,
                    name="Legacy on_register_jobs hook",
                    status=_status_for_active_state(is_active),
                    source="legacy.on_register_jobs",
                )
            )
        return records

    def _overrides_addon_base_method(self, instance: Any, method_name: str) -> bool:
        try:
            from .addon_base import AddonBase
        except Exception:
            return False
        method = getattr(type(instance), method_name, None)
        base_method = getattr(AddonBase, method_name, None)
        return method is not None and method is not base_method

    def _parse_tools(
        self,
        manifest: dict[str, Any],
        instance: Any,
        addon_id: str,
        is_active: bool,
        warnings: list[str],
    ) -> list[ToolRecord]:
        records = []
        explicit_tool_ids = set()
        explicit_legacy_names = set()
        legacy_tools = []
        legacy_tools_loaded = False

        if instance is not None and hasattr(instance, "get_tools"):
            try:
                legacy_tools = instance.get_tools() or []
                legacy_tools_loaded = True
            except Exception as exc:
                warnings.append(f"Failed to infer tools for addon '{addon_id}': {exc}")

        legacy_declarations_by_name = {}
        for declaration in legacy_tools:
            if not isinstance(declaration, dict):
                continue
            function = declaration.get("function") or {}
            legacy_name = function.get("name")
            if legacy_name:
                legacy_declarations_by_name[legacy_name] = declaration

        for tool in _normalize_record_list(manifest.get("tools")):
            stable_id = _qualified_id(addon_id, tool.get("id") or tool.get("name"), "tool")
            explicit_tool_ids.add(stable_id)
            declared_legacy_name = tool.get("legacy_name") or tool.get("function_name")
            legacy_declaration = legacy_declarations_by_name.get(declared_legacy_name)
            function_schema = copy.deepcopy(tool.get("function_schema") or tool.get("schema") or legacy_declaration or {})
            function = function_schema.get("function") or {}
            legacy_name = declared_legacy_name or function.get("name")
            if legacy_name:
                explicit_legacy_names.add(legacy_name)
            input_schema = copy.deepcopy(tool.get("input_schema") or function_schema.get("function", {}).get("parameters") or {})
            effects = tool.get("side_effects", tool.get("effects"))
            if isinstance(effects, dict):
                effects = effects.get("categories") or effects.get("types") or effects.get("effects")
            approval = tool.get("approval")
            approval_required = bool(tool.get("approval_required", False))
            if isinstance(approval, dict):
                approval_required = bool(approval.get("required", approval_required))
            elif isinstance(approval, bool):
                approval_required = approval
            records.append(
                ToolRecord(
                    id=stable_id,
                    addon_id=addon_id,
                    name=tool.get("name", function.get("description") or stable_id),
                    legacy_name=legacy_name,
                    legacy_prefix=tool.get("legacy_prefix") or addon_id,
                    aliases=sorted({stable_id, *[str(item) for item in _normalize_list(tool.get("aliases")) if item]}),
                    function_schema=function_schema,
                    input_schema=input_schema,
                    output_schema=copy.deepcopy(tool.get("output_schema") or {}),
                    side_effects=[str(item) for item in _normalize_list(effects) if item],
                    approval_required=approval_required,
                    resource_requirements=_merge_unique(
                        tool.get("resources"),
                        tool.get("resource_requirements"),
                        tool.get("resource_scopes"),
                    ),
                    status=_status_for_active_state(is_active),
                    source="explicit.tools",
                )
            )

        if instance is None or not hasattr(instance, "get_tools"):
            return records

        if not legacy_tools_loaded:
            return records

        legacy_prefix = getattr(instance, "prefix", addon_id)
        for declaration in legacy_tools:
            if not isinstance(declaration, dict):
                continue
            function = declaration.get("function") or {}
            legacy_name = function.get("name")
            if not legacy_name:
                continue
            if legacy_name in explicit_legacy_names:
                continue
            local_name = _safe_id(_strip_legacy_prefix(legacy_name, legacy_prefix, addon_id))
            stable_id = f"{addon_id}.{local_name}"
            if stable_id in explicit_tool_ids:
                continue
            records.append(
                ToolRecord(
                    id=stable_id,
                    addon_id=addon_id,
                    name=function.get("description") or local_name.replace("_", " ").title(),
                    legacy_name=legacy_name,
                    legacy_prefix=legacy_prefix,
                    aliases=sorted({stable_id, legacy_name, local_name}),
                    function_schema=copy.deepcopy(declaration),
                    input_schema=copy.deepcopy(function.get("parameters") or {}),
                    side_effects=[],
                    approval_required=False,
                    resource_requirements=[],
                    status=_status_for_active_state(is_active),
                    source="legacy.get_tools",
                )
            )
        return records

    def _parse_tool_views(self, manifest: dict[str, Any], addon_id: str, is_active: bool) -> list[ToolViewRecord]:
        records = []
        for view in _normalize_record_list(manifest.get("tool_views")):
            tool_id = view.get("tool_id") or view.get("tool")
            if tool_id and "." not in str(tool_id):
                tool_id = f"{addon_id}.{tool_id}"
            view_id = _qualified_id(addon_id, view.get("id") or view.get("name"), "tool_view")
            records.append(
                ToolViewRecord(
                    id=view_id,
                    tool_id=tool_id,
                    name=view.get("name", view_id),
                    allowed_parameters=[str(item) for item in _normalize_list(view.get("allowed_parameters")) if item],
                    description=view.get("description", ""),
                    code_callable=_truthy_manifest_value(
                        view.get("code_callable", view.get("scriptable")),
                        default=False,
                    ),
                    batch_policy=str(view.get("batch_policy") or "serial"),
                    idempotency=str(view.get("idempotency") or view.get("idempotency_policy") or "unknown"),
                    limits=copy.deepcopy(view.get("limits") or {}),
                    status=_status_for_active_state(is_active),
                )
            )
        return records


class _RecordRegistry:
    def __init__(self):
        self.records: dict[str, Any] = {}
        self.aliases: dict[str, str] = {}

    def clear(self):
        self.records.clear()
        self.aliases.clear()

    def register(self, record: Any, aliases: list[str] | None = None):
        self.records[record.id] = record
        for alias in aliases or getattr(record, "aliases", []) or []:
            self.aliases[str(alias)] = record.id

    def get(self, record_id: str) -> Any | None:
        resolved_id = self.aliases.get(record_id, record_id)
        return self.records.get(resolved_id)

    def list(self) -> list[Any]:
        return list(self.records.values())

    def as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.list()]


class ToolRegistry(_RecordRegistry):
    def __init__(self):
        super().__init__()
        self.legacy_bindings: dict[str, str] = {}
        self.tool_views: dict[str, ToolViewRecord] = {}

    def clear(self):
        super().clear()
        self.legacy_bindings.clear()
        self.tool_views.clear()

    def register(self, record: ToolRecord, aliases: list[str] | None = None):
        super().register(record, aliases)
        if record.legacy_name:
            self.legacy_bindings[record.legacy_name] = record.id
            self.aliases[record.legacy_name] = record.id

    def register_view(self, record: ToolViewRecord):
        self.tool_views[record.id] = record

    def resolve(self, tool_id_or_legacy_name: str) -> ToolRecord | None:
        return self.get(tool_id_or_legacy_name)

    def generate_tool_declarations(
        self,
        tool_ids: list[str] | None = None,
        view_constraints: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Generate declarations; ``None`` selects all tools and ``[]`` selects none."""
        view_constraints = view_constraints or {}
        records = (
            [self.resolve(tool_id) for tool_id in tool_ids]
            if tool_ids is not None
            else self.list()
        )
        declarations = []
        for record in records:
            if not record or record.status.state not in {ACTIVE, DEGRADED}:
                continue
            declaration = copy.deepcopy(record.function_schema)
            if not declaration:
                declaration = {
                    "type": "function",
                    "function": {
                        "name": record.legacy_name or record.id.replace(".", "_"),
                        "description": record.name,
                        "parameters": record.input_schema or {"type": "object", "properties": {}},
                    },
                }
            allowed = view_constraints.get(record.id) or view_constraints.get(record.legacy_name or "")
            if allowed:
                declaration = self._declaration_for_allowed_parameters(declaration, allowed)
            declaration = self._declaration_with_runtime_control_parameters(declaration, record)
            declarations.append(declaration)
        return declarations

    def declaration_for_view(self, view_id: str) -> dict[str, Any] | None:
        view = self.tool_views.get(view_id)
        if not view:
            return None
        declarations = self.generate_tool_declarations([view.tool_id], {view.tool_id: view.allowed_parameters})
        return declarations[0] if declarations else None

    def validate_tool_call(
        self,
        tool_id_or_legacy_name: str,
        arguments: dict[str, Any],
        allowed_parameters: list[str] | None = None,
    ) -> dict[str, Any]:
        record = self.resolve(tool_id_or_legacy_name)
        if not record:
            raise KeyError(f"Tool '{tool_id_or_legacy_name}' is not registered.")
        if record.status.state not in {ACTIVE, DEGRADED}:
            raise RuntimeError(f"Tool '{record.id}' is {record.status.state}.")
        if allowed_parameters:
            unknown = sorted(set(arguments) - set(allowed_parameters) - RUNTIME_CONTROL_ARGUMENTS)
            if unknown:
                raise ValueError(f"Tool call includes parameters outside the selected tool view: {', '.join(unknown)}")
            return {key: value for key, value in arguments.items() if key in set(allowed_parameters) | RUNTIME_CONTROL_ARGUMENTS}
        return dict(arguments)

    def _declaration_with_runtime_control_parameters(
        self,
        declaration: dict[str, Any],
        record: ToolRecord,
    ) -> dict[str, Any]:
        if not record.approval_required and not record.resource_requirements:
            return declaration

        function = declaration.get("function") or {}
        parameters = copy.deepcopy(function.get("parameters") or {})
        properties = dict(parameters.get("properties") or {})
        if record.approval_required:
            properties.setdefault(
                "_approval_id",
                {
                    "type": "string",
                    "description": "Runtime approval token returned by a prior blocked call. Omit until the user approves the request.",
                },
            )
        if record.resource_requirements:
            properties.setdefault(
                "_resource_reference_ids",
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Runtime resource references authorizing the selected private resource scope.",
                },
            )
        parameters["properties"] = properties
        function["parameters"] = parameters
        declaration["function"] = function
        return declaration

    def _declaration_for_allowed_parameters(
        self,
        declaration: dict[str, Any],
        allowed_parameters: list[str],
    ) -> dict[str, Any]:
        allowed = set(allowed_parameters)
        function = declaration.get("function") or {}
        parameters = copy.deepcopy(function.get("parameters") or {})
        properties = parameters.get("properties") or {}
        parameters["properties"] = {key: value for key, value in properties.items() if key in allowed}
        if "required" in parameters:
            parameters["required"] = [key for key in parameters["required"] if key in allowed]
        function["parameters"] = parameters
        declaration["function"] = function
        return declaration


class HelperRegistry(_RecordRegistry):
    def resolve(self, helper_id_or_alias: str) -> HelperRecord | None:
        return self.get(helper_id_or_alias)

    def get_legacy_details(self) -> list[dict[str, Any]]:
        details = []
        for record in sorted(self.list(), key=lambda item: item.name):
            if record.status.state not in {ACTIVE, DEGRADED}:
                continue
            details.append(
                {
                    "id": record.addon_id,
                    "addon_id": record.addon_id,
                    "stable_id": record.id,
                    "helper_id": record.helper_id,
                    "name": record.name,
                    "description": record.description,
                    "view_func": record.view_func,
                    "icon": record.icon,
                    "template": record.template,
                    "css_assets": record.css_assets,
                    "js_assets": record.js_assets,
                    "load_on_chat_page": record.load_on_chat_page,
                    "css_on_chat_page": record.css_on_chat_page,
                    "status": asdict(record.status),
                    "aliases": record.aliases,
                }
            )
        return details


class ProviderRegistry(_RecordRegistry):
    def by_domain(self, domain: str) -> list[ProviderRecord]:
        return [record for record in self.list() if record.domain == domain]


class SkillRegistry(_RecordRegistry):
    def resolve(self, skill_id_or_alias: str) -> SkillRecord | None:
        return self.get(skill_id_or_alias)

    def available(self) -> list[SkillRecord]:
        return [record for record in self.list() if record.status.state in AVAILABLE_ELEMENT_STATUSES]

    def generate_tool_declarations(
        self,
        skill_id: str,
        tools: ToolRegistry,
        open_mode: bool = False,
    ) -> list[dict[str, Any]]:
        skill = self.resolve(skill_id)
        if not skill or skill.status.state not in AVAILABLE_ELEMENT_STATUSES:
            return []

        tool_ids = list(skill.tool_ids or skill.required_tools)
        view_constraints: dict[str, list[str]] = {}
        for view_id in skill.tool_view_ids:
            view = tools.tool_views.get(view_id)
            if not view:
                continue
            if view.tool_id not in tool_ids:
                tool_ids.append(view.tool_id)
            if not open_mode and view.allowed_parameters:
                view_constraints[view.tool_id] = view.allowed_parameters
        if not tool_ids:
            return []
        return tools.generate_tool_declarations(tool_ids, view_constraints)

    def prompt_snippets(self) -> list[str]:
        snippets = []
        for record in sorted(self.available(), key=lambda item: item.name):
            snippets.append(f"- {record.id}: {record.name} - {record.description}")
        return snippets

    def slash_commands(self) -> list[dict[str, Any]]:
        commands = []
        for record in sorted(self.available(), key=lambda item: item.name):
            commands.append(
                {
                    "command": "skill",
                    "label": f"skill {record.id}",
                    "insert_text": f"/skill {record.id} ",
                    "description": f"Use skill: {record.name}",
                    "search_text": f"skill {record.id} {record.name} {record.description}",
                    "skill_id": record.id,
                    "is_prompt_passthrough": True,
                }
            )
        return commands


class WorkflowRegistry(_RecordRegistry):
    def resolve(self, workflow_id_or_alias: str) -> WorkflowRecord | None:
        return self.get(workflow_id_or_alias)

    def available(self) -> list[WorkflowRecord]:
        return [record for record in self.list() if record.status.state in AVAILABLE_ELEMENT_STATUSES]

    def slash_commands(self) -> list[dict[str, Any]]:
        commands = []
        for record in sorted(self.available(), key=lambda item: item.name):
            mode = "dry-run " if record.dry_run_supported else ""
            commands.append(
                {
                    "command": "workflow",
                    "label": f"workflow {record.id}",
                    "insert_text": f"/workflow {record.id} {mode}",
                    "description": f"Discover workflow: {record.name}",
                    "search_text": f"workflow {record.id} {record.name} {record.description}",
                    "workflow_id": record.id,
                    "is_prompt_passthrough": True,
                }
            )
        return commands


class CapabilityRegistry(_RecordRegistry):
    def rebuild(
        self,
        packages: dict[str, AddonPackageRecord],
        tools: ToolRegistry,
        helpers: HelperRegistry,
        ui_contributions: _RecordRegistry,
        providers: ProviderRegistry,
        prompts: list[PromptRecord],
        guidance: list[GuidanceRecord],
        skills: SkillRegistry,
        workflows: WorkflowRegistry,
        agent_profiles: _RecordRegistry,
    ):
        self.clear()
        related_by_target: dict[str, list[str]] = {}

        self._register_core_capabilities()
        prompt_ids_by_addon: dict[str, list[str]] = {}
        for prompt in prompts:
            prompt_ids_by_addon.setdefault(prompt.addon_id, []).append(prompt.id)

        guidance_ids_by_target: dict[str, list[str]] = {}
        for guidance_record in guidance:
            if guidance_record.status.state not in AVAILABLE_ELEMENT_STATUSES:
                continue
            targets = guidance_record.applies_to or [guidance_record.addon_id]
            for target in targets:
                guidance_ids_by_target.setdefault(target, []).append(guidance_record.id)

        tools_by_addon: dict[str, list[ToolRecord]] = {}
        for tool in tools.list():
            tools_by_addon.setdefault(tool.addon_id, []).append(tool)

        helpers_by_addon: dict[str, list[HelperRecord]] = {}
        for helper in helpers.list():
            helpers_by_addon.setdefault(helper.addon_id, []).append(helper)

        ui_contributions_by_addon: dict[str, list[UiContributionRecord]] = {}
        for contribution in ui_contributions.list():
            ui_contributions_by_addon.setdefault(contribution.addon_id, []).append(contribution)

        providers_by_addon: dict[str, list[ProviderRecord]] = {}
        for provider in providers.list():
            providers_by_addon.setdefault(provider.addon_id, []).append(provider)

        skills_by_addon: dict[str, list[SkillRecord]] = {}
        for skill in skills.list():
            if skill.addon_id:
                skills_by_addon.setdefault(skill.addon_id, []).append(skill)

        workflows_by_addon: dict[str, list[WorkflowRecord]] = {}
        for workflow in workflows.list():
            if workflow.addon_id:
                workflows_by_addon.setdefault(workflow.addon_id, []).append(workflow)

        profiles_by_addon: dict[str, list[AgentProfileRecord]] = {}
        for profile in agent_profiles.list():
            if profile.addon_id:
                profiles_by_addon.setdefault(profile.addon_id, []).append(profile)

        for addon_id, package in packages.items():
            addon_tools = tools_by_addon.get(addon_id, [])
            if addon_tools:
                self.register(
                    CapabilityRecord(
                        id=f"{addon_id}.toolkit",
                        addon_id=addon_id,
                        capability_type="toolkit",
                        name=package.name,
                        description=package.description,
                        element_ids=[tool.id for tool in addon_tools],
                        prompts=prompt_ids_by_addon.get(addon_id, []),
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{addon_id}.toolkit", addon_id),
                        status=package.status,
                        group="Toolkits",
                        label=package.name,
                        selection_mode="direct_toolkit",
                        source_addon_id=addon_id,
                        related_capability_ids=self._related_ids(related_by_target, f"{addon_id}.toolkit", addon_id),
                    )
                )

            for helper in helpers_by_addon.get(addon_id, []):
                capability_type = "ui_component" if helper.source == "explicit.ui_components" else "helper"
                self.register(
                    CapabilityRecord(
                        id=f"{helper.id}.capability",
                        addon_id=addon_id,
                        capability_type=capability_type,
                        name=helper.name,
                        description=helper.description,
                        element_ids=[helper.id],
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{helper.id}.capability", addon_id),
                        status=helper.status,
                        group="Core" if capability_type == "ui_component" else "Toolkits",
                        label=helper.name,
                        selection_mode="ui_component",
                        source_addon_id=addon_id,
                    )
                )

            for contribution in ui_contributions_by_addon.get(addon_id, []):
                capability_type = "workspace" if contribution.kind == "workspace" else "ui_contribution"
                element_ids = [contribution.id]
                if contribution.component_id:
                    element_ids.append(contribution.component_id)
                if contribution.workflow_id:
                    element_ids.append(contribution.workflow_id)
                self.register(
                    CapabilityRecord(
                        id=f"{contribution.id}.capability",
                        addon_id=addon_id,
                        capability_type=capability_type,
                        name=contribution.title,
                        description=contribution.description,
                        element_ids=element_ids,
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{contribution.id}.capability", addon_id),
                        status=contribution.status,
                        group="Workflows" if contribution.workflow_id or contribution.kind == "workspace" else "Core",
                        label=contribution.title,
                        selection_mode="launch",
                        source_addon_id=addon_id,
                        ui_entry_point=contribution.entrypoints[0] if contribution.entrypoints else {},
                        launch={"surfaces": contribution.surfaces, "entrypoints": contribution.entrypoints},
                    )
                )

            for provider in providers_by_addon.get(addon_id, []):
                provider_description = " ".join(filter(None, [
                    provider.name,
                    f"({provider.domain})" if provider.domain else None,
                    f"Operations: {', '.join(provider.operations)}" if provider.operations else None,
                ]))
                self.register(
                    CapabilityRecord(
                        id=f"{provider.id}.capability",
                        addon_id=addon_id,
                        capability_type="provider",
                        name=provider.name,
                        description=provider_description or package.description,
                        element_ids=[provider.id, *provider.service_ids],
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{provider.id}.capability", addon_id),
                        status=provider.status,
                        group="Core",
                        label=provider.name,
                        selection_mode="provider",
                        source_addon_id=addon_id,
                    )
                )

            for skill in skills_by_addon.get(addon_id, []):
                self.register(
                    CapabilityRecord(
                        id=f"{skill.id}.capability",
                        addon_id=addon_id,
                        capability_type="skill",
                        name=skill.name,
                        description=skill.description,
                        element_ids=[skill.id, *skill.tool_ids, *skill.tool_view_ids, *skill.helper_ids],
                        prompts=skill.prompt_ids,
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{skill.id}.capability", addon_id),
                        status=skill.status,
                        group="Skills",
                        label=skill.name,
                        selection_mode="skill",
                        source_addon_id=addon_id,
                    )
                )

            for workflow in workflows_by_addon.get(addon_id, []):
                self.register(
                    CapabilityRecord(
                        id=f"{workflow.id}.capability",
                        addon_id=addon_id,
                        capability_type="workflow",
                        name=workflow.name,
                        description=workflow.description,
                        element_ids=[workflow.id, *workflow.ui_surface_ids],
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{workflow.id}.capability", addon_id),
                        status=workflow.status,
                        group="Workflows",
                        label=workflow.name,
                        selection_mode="workflow",
                        source_addon_id=addon_id,
                        launch={"launch_url": workflow.launch_url, "dry_run_supported": workflow.dry_run_supported},
                    )
                )

            for profile in profiles_by_addon.get(addon_id, []):
                element_ids = [profile.id, *profile.skill_ids, *profile.workflow_ids, *profile.tool_ids]
                self.register(
                    CapabilityRecord(
                        id=f"{profile.id}.capability",
                        addon_id=addon_id,
                        capability_type="agent_profile",
                        name=profile.name,
                        description=profile.description,
                        element_ids=element_ids,
                        prompts=profile.prompt_ids,
                        guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{profile.id}.capability", addon_id),
                        status=profile.status,
                        group="Agent Profiles",
                        label=profile.name,
                        selection_mode="agent_profile",
                        source_addon_id=addon_id,
                        related_capability_ids=self._related_ids(related_by_target, f"{profile.id}.capability", addon_id),
                        schema={"model_class": profile.model_class} if profile.model_class else {},
                    )
                )

        for skill in skills.list():
            if skill.addon_id:
                continue
            self.register(
                CapabilityRecord(
                    id=f"{skill.id}.capability",
                    addon_id="personal",
                    capability_type="skill",
                    name=skill.name,
                    description=skill.description,
                    element_ids=[skill.id, *skill.tool_ids, *skill.tool_view_ids, *skill.helper_ids],
                    prompts=skill.prompt_ids,
                    guidance_ids=self._guidance_ids(guidance_ids_by_target, f"{skill.id}.capability", "personal"),
                    status=skill.status,
                    group="Skills",
                    label=skill.name,
                    selection_mode="skill",
                    source_addon_id="personal",
                )
            )

    def _register_core_capabilities(self):
        core_capabilities = [
            ("capabilities.search", "Search Capabilities", "Find compact candidate capability cards for a task."),
            ("capabilities.runtime_inventory", "Runtime Inventory", "Return compact runtime affordances for planning a task."),
            ("capabilities.source_coverage", "Source Coverage", "Return compact source-class coverage rows for multi-repository tasks."),
            ("capabilities.describe", "Describe Capabilities", "Return operational details for selected capabilities."),
            ("capabilities.get_guidance", "Get Guidance", "Fetch full guidance content only after a relevant capability is selected."),
            ("capabilities.load_to_session", "Load to Session", "Dynamically registers and injects full tool schemas into the active session."),
            ("runs.upgrade_session_to_run", "Upgrade to Run", "Converts the current inline chat session into a stateful Workflow Run."),
        ]
        for capability_id, name, description in core_capabilities:
            self.register(
                CapabilityRecord(
                    id=capability_id,
                    addon_id="core",
                    capability_type="core",
                    name=name,
                    description=description,
                    status=ElementStatus(ACTIVE),
                    source="core.meta_capability",
                    group="Core",
                    label=name,
                    selection_mode="always_available",
                    source_addon_id="core",
                )
            )

    def _related_ids(self, related_by_target: dict[str, list[str]], capability_id: str, addon_id: str) -> list[str]:
        return _merge_unique(related_by_target.get(capability_id, []), related_by_target.get(addon_id, []))

    def _guidance_ids(self, guidance_ids_by_target: dict[str, list[str]], capability_id: str, addon_id: str) -> list[str]:
        return _merge_unique(guidance_ids_by_target.get(capability_id, []), guidance_ids_by_target.get(addon_id, []))


class AddonRegistries:
    def __init__(self):
        self.packages: dict[str, AddonPackageRecord] = {}
        self.tools = ToolRegistry()
        self.helpers = HelperRegistry()
        self.ui_contributions = _RecordRegistry()
        self.providers = ProviderRegistry()
        self.skills = SkillRegistry()
        self.workflows = WorkflowRegistry()
        self.capabilities = CapabilityRegistry()
        self.ui_surfaces = _RecordRegistry()
        self.services = _RecordRegistry()
        self.workers = _RecordRegistry()
        self.scheduled_jobs = _RecordRegistry()
        self.agent_profiles = _RecordRegistry()
        self.prompts: list[PromptRecord] = []
        self.guidance: list[GuidanceRecord] = []
        self.warnings: list[str] = []

    def clear(self):
        self.packages.clear()
        self.tools.clear()
        self.helpers.clear()
        self.ui_contributions.clear()
        self.providers.clear()
        self.skills.clear()
        self.workflows.clear()
        self.capabilities.clear()
        self.ui_surfaces.clear()
        self.services.clear()
        self.workers.clear()
        self.scheduled_jobs.clear()
        self.agent_profiles.clear()
        self.prompts.clear()
        self.guidance.clear()
        self.warnings.clear()

    def register_elements(self, elements: AddonElements):
        self.packages[elements.package.id] = elements.package
        for tool in elements.tools:
            self.tools.register(tool)
        for tool_view in elements.tool_views:
            self.tools.register_view(tool_view)
        for helper in elements.helpers:
            self.helpers.register(helper)
        for contribution in elements.ui_contributions:
            self.ui_contributions.register(contribution)
        for provider in elements.providers:
            self.providers.register(provider)
        for surface in elements.ui_surfaces:
            self.ui_surfaces.register(surface)
        for service in elements.services:
            self.services.register(service)
        for worker in elements.workers:
            self.workers.register(worker)
        for job in elements.scheduled_jobs:
            self.scheduled_jobs.register(job)
        for skill in elements.skills:
            self.skills.register(skill)
        for workflow in elements.workflows:
            self.workflows.register(workflow)
        for profile in elements.agent_profiles:
            self.agent_profiles.register(profile)
        self.prompts.extend(elements.prompts)
        self.guidance.extend(elements.guidance)
        self.warnings.extend(elements.warnings)

    def rebuild_availability(self):
        for skill in self.skills.list():
            self._apply_requirement_status(skill)
        for workflow in self.workflows.list():
            self._apply_requirement_status(workflow)
        for contribution in self.ui_contributions.list():
            self._apply_requirement_status(contribution)
        for profile in self.agent_profiles.list():
            self._apply_agent_profile_status(profile)

    def rebuild_capabilities(self):
        self.rebuild_availability()
        self.capabilities.rebuild(
            self.packages,
            self.tools,
            self.helpers,
            self.ui_contributions,
            self.providers,
            self.prompts,
            self.guidance,
            self.skills,
            self.workflows,
            self.agent_profiles,
        )

    def _apply_agent_profile_status(self, profile: AgentProfileRecord):
        if profile.status.state not in AVAILABLE_ELEMENT_STATUSES:
            return

        missing = []
        degraded = []
        self._collect_registry_requirement_status(profile.skill_ids, self.skills, "skill", missing, degraded)
        self._collect_registry_requirement_status(profile.workflow_ids, self.workflows, "workflow", missing, degraded)
        self._collect_registry_requirement_status(profile.tool_ids, self.tools, "tool", missing, degraded, resolver="resolve")
        self._collect_prompt_requirement_status(profile.prompt_ids, missing)
        self._set_requirement_status(profile, missing, degraded)

    def _apply_requirement_status(self, record: SkillRecord | WorkflowRecord):
        if record.status.state not in AVAILABLE_ELEMENT_STATUSES:
            return

        missing = []
        degraded = []
        self._collect_package_requirement_status(record.required_addons, missing, degraded)
        self._collect_registry_requirement_status(record.required_tools, self.tools, "tool", missing, degraded, resolver="resolve")
        self._collect_tool_view_requirement_status(record.required_tool_views, missing, degraded)
        self._collect_registry_requirement_status(record.required_helpers, self.helpers, "helper", missing, degraded, resolver="resolve")
        self._collect_registry_requirement_status(record.required_providers, self.providers, "provider", missing, degraded)
        self._collect_registry_requirement_status(record.required_services, self.services, "service", missing, degraded)
        self._collect_registry_requirement_status(record.required_workers, self.workers, "worker", missing, degraded)
        self._set_requirement_status(record, missing, degraded)

    def _set_requirement_status(self, record: Any, missing: list[str], degraded: list[str]):
        if missing:
            record.status = ElementStatus(MISSING_DEPENDENCY, [*record.status.reasons, *missing])
        elif degraded:
            record.status = ElementStatus(DEGRADED, [*record.status.reasons, *degraded])

    def _collect_package_requirement_status(
        self,
        addon_ids: list[str],
        missing: list[str],
        degraded: list[str],
    ):
        for addon_id in addon_ids:
            package = self.packages.get(addon_id)
            if not package:
                missing.append(f"Required addon '{addon_id}' is not installed.")
            elif package.status.state not in AVAILABLE_ELEMENT_STATUSES:
                missing.append(f"Required addon '{addon_id}' is {package.status.state}.")
            elif package.status.state == DEGRADED:
                degraded.append(f"Required addon '{addon_id}' is degraded: {'; '.join(package.status.reasons)}")

    def _collect_registry_requirement_status(
        self,
        record_ids: list[str],
        registry: _RecordRegistry,
        label: str,
        missing: list[str],
        degraded: list[str],
        resolver: str = "get",
    ):
        resolve = getattr(registry, resolver)
        for record_id in record_ids:
            dependency = resolve(record_id)
            if not dependency:
                missing.append(f"Required {label} '{record_id}' is not registered.")
            elif dependency.status.state not in AVAILABLE_ELEMENT_STATUSES:
                missing.append(f"Required {label} '{record_id}' is {dependency.status.state}.")
            elif dependency.status.state == DEGRADED:
                degraded.append(f"Required {label} '{record_id}' is degraded: {'; '.join(dependency.status.reasons)}")

    def _collect_tool_view_requirement_status(
        self,
        view_ids: list[str],
        missing: list[str],
        degraded: list[str],
    ):
        for view_id in view_ids:
            view = self.tools.tool_views.get(view_id)
            if not view:
                missing.append(f"Required tool view '{view_id}' is not registered.")
            elif view.status.state not in AVAILABLE_ELEMENT_STATUSES:
                missing.append(f"Required tool view '{view_id}' is {view.status.state}.")
            elif view.status.state == DEGRADED:
                degraded.append(f"Required tool view '{view_id}' is degraded: {'; '.join(view.status.reasons)}")

    def _collect_prompt_requirement_status(self, prompt_ids: list[str], missing: list[str]):
        registered = {prompt.id for prompt in self.prompts}
        for prompt_id in prompt_ids:
            if prompt_id not in registered:
                missing.append(f"Required prompt '{prompt_id}' is not registered.")

    def as_manifest(self) -> dict[str, Any]:
        return {
            "packages": [asdict(record) for record in self.packages.values()],
            "tools": self.tools.as_dicts(),
            "helpers": self.helpers.as_dicts(),
            "ui_components": self.helpers.as_dicts(),
            "ui_contributions": self.ui_contributions.as_dicts(),
            "providers": self.providers.as_dicts(),
            "skills": self.skills.as_dicts(),
            "workflows": self.workflows.as_dicts(),
            "capabilities": self.capabilities.as_dicts(),
            "ui_surfaces": self.ui_surfaces.as_dicts(),
            "services": self.services.as_dicts(),
            "workers": self.workers.as_dicts(),
            "scheduled_jobs": self.scheduled_jobs.as_dicts(),
            "agent_profiles": self.agent_profiles.as_dicts(),
            "prompts": [asdict(record) for record in self.prompts],
            "guidance": [asdict(record) for record in self.guidance],
            "warnings": list(self.warnings),
        }
