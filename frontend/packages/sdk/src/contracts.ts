export const FRONTEND_API_VERSION = "1.0" as const;
export const SUPPORTED_FRONTEND_API_MAJORS = [1] as const;

export type FrontendApiVersion = typeof FRONTEND_API_VERSION;
export type FrontendFeatureMap = Readonly<Record<string, boolean>>;

export interface FrontendProductIdentity {
  name: string;
  short_name: string;
  descriptor: string;
  mark: string;
  distribution_name: string;
  cli_name: string;
  environment_prefix: string;
  import_package: string;
  repository_url: string;
}

export interface FrontendUserSummary {
  id: string;
  display_name: string;
  is_admin: boolean;
}

export interface FrontendApplication {
  profile: "workbench" | "documents";
  label: string;
  home_url: string;
}

export interface FrontendNavigationItem {
  id: string;
  label: string;
  href: string;
  icon: string;
  section: "primary" | "utility" | string;
  client_route: boolean;
}

export interface FrontendCoreBuild {
  id: string | null;
  status: string;
  entry_url: string | null;
  css_urls: string[];
}

export interface FrontendAddonDescriptor {
  id: string;
  name: string;
  version: string;
  active: boolean;
  tier: string | null;
  frontend_api_range: string | null;
  compatible: boolean;
  module_url: string | null;
  css_urls: string[];
  legacy_routes: string[];
  build: {
    status: string;
    rebuild_needed: boolean;
    log_source_id: string | null;
  };
}

export interface FrontendUiContribution {
  id: string;
  addon_id: string;
  kind: string;
  title: string;
  description: string;
  routes: string[];
  entrypoint_kinds: string[];
  component_id: string | null;
  section: string;
  icon: string;
  status: string;
}

export type FrontendJsonSchema = Readonly<Record<string, unknown>>;

export interface FrontendUiComponentDescriptor {
  id: string;
  addon_id: string;
  name: string;
  description: string;
  frontend_api_range: string | null;
  compatible: boolean;
  module_url: string | null;
  css_urls: string[];
  input_schema_id: string | null;
  output_schema_id: string | null;
  input_schema: FrontendJsonSchema | null;
  output_schema: FrontendJsonSchema | null;
  legacy: {
    css_urls: string[];
    js_urls: string[];
    global_names: string[];
  };
  status: string;
}

export interface FrontendBootstrap {
  frontend_api_version: FrontendApiVersion;
  supported_frontend_api_majors: number[];
  product: FrontendProductIdentity;
  /** Optional for compatibility with earlier frontend API v1 servers. */
  application?: FrontendApplication;
  current_path: string;
  user: FrontendUserSummary;
  feature_flags: FrontendFeatureMap;
  navigation: FrontendNavigationItem[];
  capabilities: Readonly<Record<string, boolean>>;
  runtime_readiness: {
    state: string;
    phase: string;
    started_at: string | null;
    completed_at: string | null;
    elapsed_ms: number;
    timings_ms: Readonly<Record<string, number>>;
    details: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
    errors: Array<{
      phase: string;
      message: string;
    }>;
  };
  build: {
    core: FrontendCoreBuild;
  };
  addons: FrontendAddonDescriptor[];
  ui_contributions: FrontendUiContribution[];
  ui_components: FrontendUiComponentDescriptor[];
}

export interface FrontendProfile {
  email: string;
  is_admin: boolean;
  preferences: string;
  style_reference: string;
  security: {
    envelope_version: number;
    active_key_id: string;
    key_backend: string;
    persistent: boolean;
    automation_ready: boolean;
    recovery_export_supported: boolean;
    note: string;
  };
}

export interface FrontendProfileUpdate {
  preferences: string;
  style_reference: string;
}

export interface FrontendAddonStatus {
  state: string;
  reasons: string[];
}

export interface FrontendAddonBuildStatus {
  declared?: boolean;
  addon_id?: string;
  tier?: string | null;
  status?: string;
  rebuild_needed?: boolean;
  source_available?: boolean;
  reasons?: string[];
  warnings?: string[];
  source_dir?: string | null;
  command?: string | string[] | null;
  watch_command?: string | string[] | null;
  output_paths?: string[];
  missing_outputs?: string[];
  stamp_path?: string | null;
  stamp_current?: boolean;
  validation_mode?: "metadata" | "full";
  log_source_id?: string | null;
}

export interface FrontendAddonWorkerState {
  status: string;
  configured: number;
  running: number;
  pids: number[];
  workers: Readonly<Record<string, unknown>>[];
  log_source_id: string | null;
  desired?: number | null;
  entrypoint?: string | null;
  execution_environment?: string | null;
  allow_environment_fallback?: boolean | null;
}

export interface FrontendAddonScheduledJob {
  id: string;
  name: string;
  trigger: string;
  next_run_at: string | null;
  paused: boolean;
  pending: boolean;
}

export interface FrontendAddonScheduledJobState {
  status: string;
  declared: number;
  registered: number;
  paused: number;
  jobs: FrontendAddonScheduledJob[];
  declared_jobs: Readonly<Record<string, unknown>>[];
  log_source_id: string | null;
}

export interface FrontendAddonLogLink {
  id: string;
  name: string;
  category: string;
  description?: string;
  url: string;
  api_url?: string;
}

export interface FrontendAddonHubRelation {
  relation: "hub" | "spoke" | "standalone" | string;
  hub_id: string | null;
  hub_label: string | null;
  spoke_count: number;
}

export interface FrontendAddonPermission {
  id: string;
  reason?: string;
}

export interface FrontendAddonResource {
  id: string;
  access?: string[];
  description?: string;
}

export interface FrontendAddonDependencyNotice {
  id: string;
  packages: string[];
  license: string;
  purpose: string;
  review_required: boolean;
  source_url?: string;
}

export type FrontendAddonKind =
  | "application"
  | "toolkit"
  | "connector"
  | "engine"
  | "component";

export type FrontendAddonDistributionChannel =
  | "built_in"
  | "first_party"
  | "community"
  | "private"
  | "local_development";

export interface FrontendAddonDistribution {
  channel?: FrontendAddonDistributionChannel | string;
  publisher?: string;
  source_uri?: string;
}

export interface FrontendAddonRuntimeUnit {
  id: string;
  mode: "in_process" | "managed_process" | "container" | "remote" | string;
  entry_point?: { module?: string; class?: string } | null;
  lifecycle?: string;
  requirements?: Readonly<Record<string, unknown>>;
  config?: Readonly<Record<string, unknown>>;
}

export interface FrontendAddonDatabaseMigration {
  format: "alembic_python_v1" | string;
  version_table: string;
  purge_tables: string[];
  owned_tables: string[];
  base_revision: string;
  head_revision: string;
  revision_ids: string[];
  revision_count: number;
  graph_sha256: string;
}

export interface FrontendAddonCapabilityRequirement {
  id: string;
  capability: string;
  version: string;
  operations: string[];
  cardinality: "one" | "many" | "optional" | string;
  activation: "install" | "activate" | "on_first_use" | string;
  scope: "installation" | "user" | "workspace" | "workflow" | string;
  qualifiers?: Readonly<Record<string, unknown>>;
}

export interface FrontendAddonCapabilityProvider {
  id: string;
  capability: string;
  version: string;
  operations: string[];
  runtime_unit?: string;
  connector_definition?: string;
  setup?: Readonly<{
    mode?: "oauth" | "credentials" | "derived" | "external" | "none" | string;
    url?: string;
    label?: string;
  }>;
  qualifiers?: Readonly<Record<string, unknown>>;
}

export interface FrontendAddonUiEntry {
  id: string;
  mode: "none" | "schema" | "svelte_application" | "svelte_component" | "external" | string;
  module?: string;
  route?: string;
  css_assets?: string[];
}

export interface FrontendConnectionRecord {
  id: string;
  owner_user_id: number;
  connector_addon_id: string;
  connector_definition_id: string;
  label: string;
  state: string;
  is_default: boolean;
  config: Readonly<Record<string, unknown>>;
  health_status?: string | null;
  health_checked_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  access: "owner" | "delegated" | string;
  binding_count: number;
}

export interface FrontendConnectionList {
  frontend_api_version: string;
  items: FrontendConnectionRecord[];
}

export interface FrontendCapabilityCandidate {
  addon_id: string;
  addon_version: string;
  distribution_channel: string;
  provider_id: string;
  capability: string;
  version: string;
  operations: string[];
  runtime_unit?: string;
  connector_definition?: string;
  setup?: Readonly<{
    mode?: "oauth" | "credentials" | "derived" | "external" | "none" | string;
    url?: string;
    label?: string;
  }>;
  qualifiers?: Readonly<Record<string, unknown>>;
}

export interface FrontendCapabilityBindingSelection {
  provider_addon_id: string;
  provider_capability_id: string;
  connection_id?: string | null;
  status: string;
  priority: number;
}

export interface FrontendCapabilityResolutionItem {
  requirement: FrontendAddonCapabilityRequirement;
  status: string;
  selected: FrontendCapabilityCandidate[];
  compatible: FrontendCapabilityCandidate[];
  inactive_compatible: FrontendCapabilityCandidate[];
  connection_ids: string[];
  selected_bindings: FrontendCapabilityBindingSelection[];
  message: string;
  blocks_activation: boolean;
}

export interface FrontendCapabilityResolutionReport {
  consumer_addon_id: string;
  activatable: boolean;
  inactive_provider_count: number;
  resolutions: FrontendCapabilityResolutionItem[];
  setup_requests: ReadonlyArray<Readonly<Record<string, unknown>>>;
}

export interface FrontendAddonDoctorCheck {
  id: string;
  title: string;
  status: "ok" | "info" | "warning" | "error" | string;
  summary: string;
  addon_id?: string | null;
  requirement_id?: string | null;
  connection_id?: string | null;
  details?: Readonly<Record<string, unknown>> | null;
}

export interface FrontendAddonDoctorReport {
  frontend_api_version: string;
  summary: Readonly<{
    errors: number;
    warnings: number;
    ok: number;
    info: number;
  }>;
  checks: FrontendAddonDoctorCheck[];
  runtime: Readonly<{
    container_kind: string;
    container_available: boolean;
    container_detail: string;
    wsl_distribution?: string | null;
  }>;
}

export interface FrontendAddonManagerSummary {
  detail_level: "summary";
  detail_url: string;
  id: string;
  name: string;
  version: string;
  installed_version?: string | null;
  builtin_reconciliation_required?: Readonly<{
    installed_version: string;
    package_version: string;
    error?: string;
  }> | null;
  author: string;
  description: string;
  roles: string[];
  categories: string[];
  kind?: FrontendAddonKind | string;
  distribution?: FrontendAddonDistribution;
  effective_code_access?:
    | "host_process"
    | "host_user_process"
    | "container_service"
    | "remote_service"
    | "declarative_only"
    | "unclassified"
    | string;
  connection_count: number;
  consumer_binding_count: number;
  provider_binding_count: number;
  provider_domains: string[];
  addon_type: string[];
  dependencies: string[];
  search_terms: string[];
  is_active: boolean;
  status: FrontendAddonStatus;
  frontend_tier: string | null;
  frontend_status: string | null;
  frontend_rebuild_needed: boolean;
  worker_running: number;
  has_settings: boolean;
  has_prompts: boolean;
  delegatable: boolean;
  source_kind?: "built_in" | "installed" | string;
  trust_tier?: string;
  lifecycle_state?: string;
  lifecycle_generation?: number;
  lifecycle_error?: string | null;
  code_available?: boolean;
  can_remove_code?: boolean;
  can_purge_data?: boolean;
  inferred_hub: FrontendAddonHubRelation;
}

export interface FrontendAddonInventoryCacheStatus {
  name: string;
  owner: string;
  purpose: string;
  data_classification: string;
  persistence: string;
  key_schema: string;
  namespace: string;
  maximum_entries: number;
  maximum_bytes: number | null;
  ttl_seconds: number | null;
  stale_while_revalidate_seconds: number | null;
  invalidation_triggers: string[];
  concurrency: string;
  copy_behavior: string;
  authoritative: boolean;
  safe_reset: boolean;
  generation: number;
  entries: number;
  last_invalidation_reason: string | null;
  last_invalidation_at: string | null;
  last_build_at: string | null;
  hits: number;
  misses: number;
  stale_hits: number;
  invalidations: number;
  evictions: number;
  builds: number;
  build_errors: number;
  last_build_ms: number | null;
  total_build_ms: number;
  current_bytes: number;
}

export interface FrontendAddonInventoryResponse {
  frontend_api_version: string;
  items: FrontendAddonManagerSummary[];
  count: number;
  cache: FrontendAddonInventoryCacheStatus | null;
}

export interface FrontendAddonManagerItem {
  detail_level?: "full";
  id: string;
  name: string;
  version: string;
  installed_version?: string | null;
  builtin_reconciliation_required?: Readonly<{
    installed_version: string;
    package_version: string;
    error?: string;
  }> | null;
  author: string;
  description: string;
  roles: string[];
  categories: string[];
  kind?: FrontendAddonKind | string;
  distribution?: FrontendAddonDistribution;
  runtime_units?: FrontendAddonRuntimeUnit[];
  database_migration?: FrontendAddonDatabaseMigration | null;
  effective_code_access?:
    | "host_process"
    | "host_user_process"
    | "container_service"
    | "remote_service"
    | "declarative_only"
    | "unclassified"
    | string;
  capability_requirements?: FrontendAddonCapabilityRequirement[];
  capability_providers?: FrontendAddonCapabilityProvider[];
  ui_entries?: FrontendAddonUiEntry[];
  connection_count?: number;
  consumer_binding_count?: number;
  provider_binding_count?: number;
  provider_domains: string[];
  addon_type: string[];
  dependencies: string[];
  dependency_notices?: FrontendAddonDependencyNotice[];
  is_active: boolean;
  status: FrontendAddonStatus;
  frontend_tier: string | null;
  frontend_build: FrontendAddonBuildStatus;
  worker_state: FrontendAddonWorkerState;
  scheduled_job_state: FrontendAddonScheduledJobState;
  has_settings: boolean;
  has_prompts: boolean;
  delegatable: boolean;
  source_kind?: "built_in" | "installed" | string;
  trust_tier?: string;
  lifecycle_state?: string;
  lifecycle_generation?: number;
  lifecycle_error?: string | null;
  manifest_sha256?: string | null;
  contract_sha256?: string | null;
  provenance?: Readonly<Record<string, unknown>>;
  permissions?: FrontendAddonPermission[];
  resources?: FrontendAddonResource[];
  storage?: Readonly<Record<string, string>>;
  code_available?: boolean;
  can_remove_code?: boolean;
  can_purge_data?: boolean;
  log_links: FrontendAddonLogLink[];
  inferred_hub: FrontendAddonHubRelation;
}

export interface FrontendAddonCatalogDiffSection {
  added: ReadonlyArray<Readonly<Record<string, unknown>>>;
  removed: ReadonlyArray<Readonly<Record<string, unknown>>>;
  changed: ReadonlyArray<
    Readonly<{
      before: Readonly<Record<string, unknown>>;
      after: Readonly<Record<string, unknown>>;
    }>
  >;
}

export interface FrontendAddonCatalogDiff {
  permissions: FrontendAddonCatalogDiffSection;
  resources: FrontendAddonCatalogDiffSection;
  runtime_units: FrontendAddonCatalogDiffSection;
  database_migration: FrontendAddonCatalogDiffSection;
  dependency_notices: FrontendAddonCatalogDiffSection;
  distribution: Readonly<{
    before: Readonly<Record<string, unknown>>;
    after: Readonly<Record<string, unknown>>;
    changed: boolean;
  }>;
  contract_changed: boolean;
  requires_review: boolean;
}

export interface FrontendAddonInstallationEstimates {
  download_bytes?: number;
  unpacked_image_bytes?: number;
  model_cache_bytes?: number;
  build_cache_bytes?: number;
  required_disk_bytes?: number;
  environment_bytes?: number;
  minimum_ram_bytes?: number;
  estimated_seconds?: number;
  architectures?: string[];
}

export interface FrontendAddonCatalogTarget {
  version: string;
  published_at: string;
  addon_api_requires: string;
  archive: Readonly<{
    filename: string;
    sha256: string;
    size: number;
  }>;
  contract: Readonly<{
    manifest_sha256: string;
    contract_sha256: string;
    kind: FrontendAddonKind | string;
    distribution: FrontendAddonDistribution;
    permissions: FrontendAddonPermission[];
    resources: FrontendAddonResource[];
    runtime_units: FrontendAddonRuntimeUnit[];
    installation?: FrontendAddonInstallationEstimates;
    database_migration?: FrontendAddonDatabaseMigration | null;
    dependency_notices: FrontendAddonDependencyNotice[];
    capability_providers: FrontendAddonCapabilityProvider[];
  }>;
  notes_url?: string;
}

export interface FrontendAddonCatalogPackage {
  id: string;
  name: string;
  summary: string;
  kind: FrontendAddonKind | string;
  distribution: FrontendAddonDistribution;
  license: string;
  icon_url?: string;
  installed_version?: string | null;
  latest_version?: string | null;
  update_version?: string | null;
  status:
    | "available"
    | "installed"
    | "update_available"
    | "incompatible"
    | string;
  target: FrontendAddonCatalogTarget | null;
  update_diff: FrontendAddonCatalogDiff | null;
}

export interface FrontendAddonCatalogResponse {
  frontend_api_version: string;
  configured: boolean;
  channel: string;
  publisher?: string;
  generated_at?: string;
  source_sha256?: string;
  signer_key_id?: string | null;
  packages: FrontendAddonCatalogPackage[];
}

export interface FrontendAddonAdmissionDiskCheck {
  label: string;
  path: string;
  status: "ok" | "blocked" | "unknown" | string;
  required_bytes: number;
  available_bytes: number | null;
  message: string;
}

export interface FrontendAddonAdmissionReport {
  version: number;
  allowed: boolean;
  estimated_seconds: number | null;
  costs: Readonly<{
    download_bytes: number | null;
    unpacked_image_bytes: number | null;
    model_cache_bytes: number | null;
    build_cache_bytes: number | null;
    required_disk_bytes: number | null;
  }>;
  disk: FrontendAddonAdmissionDiskCheck[];
  memory: Readonly<{
    status: string;
    required_bytes: number | null;
    available_bytes: number | null;
    message: string;
  }>;
  container_runtime: Readonly<{
    required: boolean;
    status: string;
    message: string;
  }>;
  gpu: Readonly<{
    requirement: string;
    status: string;
    message: string;
  }>;
  architecture: Readonly<{
    current: string;
    supported: string[];
    status: string;
  }>;
  warnings: string[];
  blocking_reasons: string[];
}

export interface FrontendAddonOperation {
  id: string;
  job_id: string;
  addon_id: string | null;
  operation: "install_upload" | "install_catalog" | "activate" | string;
  status: string;
  stage: string;
  progress: number | null;
  message: string | null;
  error: string | null;
  bytes: Readonly<{
    current: number | null;
    total: number | null;
  }>;
  last_activity_at: string | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  attempt: number;
  recovered_count: number;
  cancel_requested: boolean;
  cancellable: boolean;
  retryable: boolean;
  retry_expires_at: string | null;
  input_retained: boolean | null;
  admission: FrontendAddonAdmissionReport | null;
  readiness: Readonly<Record<string, unknown>>;
  result: Readonly<Record<string, unknown>> | null;
  log_source_ids: string[];
  log_links: FrontendAddonLogLink[];
  status_url: string;
  cancel_url: string;
  retry_url: string;
}

export interface FrontendAddonOperationListResponse {
  items: FrontendAddonOperation[];
  count: number;
}

export interface FrontendAddonActionResponse {
  success: boolean;
  message: string;
  addon_id?: string;
  job_id?: string;
  operation?: FrontendAddonOperation;
  requires_restart?: boolean;
  discovered_count?: number;
}

export interface FrontendAddonSettingOption {
  value: string;
  label: string;
}

export interface FrontendAddonSettingMappingColumn {
  name: string;
  kind: "text" | "combobox" | "select";
  placeholder: string;
  options: FrontendAddonSettingOption[];
  options_source: "llm_models" | null;
}

export interface FrontendAddonSettingField {
  id: string;
  name: string;
  description: string;
  kind:
    | "checkbox"
    | "combobox"
    | "mapping"
    | "model_select"
    | "number"
    | "password"
    | "select"
    | "text"
    | "textarea"
    | "url";
  scope: "global" | "user";
  scope_label: string;
  section: string;
  placeholder: string;
  required: boolean;
  sensitive: boolean;
  configured: boolean;
  editable: boolean;
  value: unknown;
  options: FrontendAddonSettingOption[];
  options_source: "llm_models" | null;
  minimum: number | null;
  maximum: number | null;
  step: number | null;
  rows: number;
  requires_restart: boolean;
  mapping: {
    key: FrontendAddonSettingMappingColumn;
    value: FrontendAddonSettingMappingColumn;
    add_label: string;
  } | null;
}

export interface FrontendAddonConnection {
  id: string;
  name: string;
  active: boolean;
  state: "ready" | "needs_attention" | "disabled" | "legacy_unverified";
  access: "owner" | "delegated";
  can_manage: boolean;
  health_status: string | null;
  created_at: string | null;
  activate_url: string | null;
  delete_url: string | null;
  delete_method: "DELETE" | "POST";
  delete_body: Readonly<Record<string, unknown>> | null;
}

export interface FrontendAddonConnectionField {
  id: string;
  name: string;
  kind: "text" | "url" | "password";
  required: boolean;
  placeholder?: string;
  description?: string;
}

export interface FrontendAddonConnectionDesk {
  definition_id: string;
  label: string;
  mode: "oauth" | "credentials" | "managed";
  delegated_from: string | null;
  items: FrontendAddonConnection[];
  connect_url: string | null;
  callback_url: string | null;
  create_url: string | null;
  create_fields: FrontendAddonConnectionField[];
  tested_on_create: boolean;
}

export interface FrontendAddonSettingsDashboard {
  frontend_api_version: FrontendApiVersion;
  addon: {
    id: string;
    name: string;
    description: string;
  };
  permissions: {
    manage_global: boolean;
  };
  fields: FrontendAddonSettingField[];
  connections: FrontendAddonConnectionDesk | null;
  actions: {
    update_url: string;
  };
}

export interface FrontendAddonSettingsMutation {
  success: boolean;
  message: string;
  restart_required: boolean;
  settings: FrontendAddonSettingsDashboard;
}

export interface FrontendModelCapabilities {
  vision: boolean;
  pdf: boolean;
  reasoning: boolean;
  audio: boolean;
  reasoning_levels: string[];
}

export interface FrontendModelLimit {
  detected_tokens: number | null;
  override_tokens: number | null;
  effective_tokens: number;
  usable_tokens: number | null;
  known: boolean;
  source: string;
  source_label: string;
  confidence: string;
  provisional: boolean;
}

export interface FrontendModelRecord {
  db_id: number;
  id: string;
  name: string;
  provider_id: number;
  provider_name: string;
  scope: "global" | "personal" | "member" | string;
  scope_label: string;
  active: boolean;
  manual: boolean;
  type: "chat" | "embedding" | string;
  context_window: number | null;
  max_output_tokens?: number | null;
  limits?: {
    input: FrontendModelLimit;
    output: FrontendModelLimit;
  };
  capabilities: FrontendModelCapabilities;
  embedding: {
    default_dimensions: number | null;
    supports_mrl: boolean;
    prefix_style: string;
    has_custom_query_prefix: boolean;
    has_custom_passage_prefix: boolean;
  };
  costs: {
    input: number | null;
    cached_input: number | null;
    output: number | null;
  };
  permissions: {
    edit: boolean;
    delete: boolean;
    toggle_active: boolean;
    toggle_capabilities: boolean;
  };
  actions: {
    update_url: string;
    delete_url: string;
    toggle_active_url: string;
    toggle_vision_url: string;
    toggle_pdf_url: string;
    toggle_reasoning_url: string;
    toggle_audio_url: string;
  };
  updated_at: string | null;
}

export interface FrontendModelProvider {
  id: number;
  key: string;
  name: string;
  scope: "global" | "personal" | string;
  scope_label: string;
  kind: string;
  kind_label: string;
  discovery: string;
  discovery_label: string;
  status: "ready" | "configured" | "attention" | string;
  status_label: string;
  connection_count: number;
  active_connection_count: number;
  model_count: number;
  active_model_count: number;
  requires_endpoint: boolean;
  supports_custom_models: boolean;
  supports_discovery: boolean;
  permissions: {
    add_connection: boolean;
    add_model: boolean;
    delete_provider: boolean;
    sync_models: boolean;
  };
  actions: {
    detail_url: string;
    connections_url: string;
    models_url: string;
    delete_url: string;
    sync_url: string;
  };
}

export interface FrontendProviderConnection {
  id: number;
  name: string;
  scope: "global" | "personal" | string;
  scope_label: string;
  active: boolean;
  endpoint_label: string | null;
  endpoint_configured: boolean;
  api_version: string | null;
  created_at: string | null;
  permissions: {
    update: boolean;
    delete: boolean;
    activate: boolean;
  };
  actions: {
    update_url: string;
    delete_url: string;
    activate_url: string;
  };
}

export interface FrontendProviderSettingsDetail {
  frontend_api_version: FrontendApiVersion;
  provider: FrontendModelProvider;
  connections: FrontendProviderConnection[];
  models: FrontendModelRecord[];
  model_total: number;
  limits: {
    connections: number;
    models: number;
  };
}

export interface FrontendIntelligenceSettingsDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  permissions: {
    manage_global: boolean;
    share_credentials: boolean;
  };
  summary: {
    providers: number;
    connected_providers: number;
    connections: number;
    active_models: number;
    routing_models: number;
    delegations: number;
  };
  providers: FrontendModelProvider[];
  routing: {
    defaults: {
      fast: string;
      medium: string;
      powerful: string;
    };
    models: FrontendModelRecord[];
    can_manage_defaults: boolean;
  };
  image_detail: {
    configs: Readonly<Record<string, string>>;
    allowed_values: string[];
  };
  links: {
    sharing: string;
    preferences: string;
    create_connection: string;
    create_endpoint: string;
    create_model: string;
  };
  limits: {
    connections_per_provider: number;
    models_per_provider: number;
    routing_models: number;
  };
}

export interface FrontendSharingCredential {
  id: number | null;
  provider_id: string;
  label: string;
  active: boolean;
  source: "owned" | "settings" | string;
  source_label: string;
  created_at: string | null;
}

export interface FrontendSharingProvider {
  id: string;
  name: string;
  configured: boolean;
  credentials: FrontendSharingCredential[];
}

export interface FrontendCredentialDelegation {
  provider_id: string;
  provider_name: string;
  credential_id: number | null;
  credential_name: string;
  delegatee_id: number;
  delegatee_email: string;
}

export interface FrontendSharingRecipient {
  id: number;
  email: string;
}

export interface FrontendCredentialSharingDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  summary: {
    providers: number;
    configured_providers: number;
    credentials: number;
    delegations: number;
    recipients: number;
  };
  providers: FrontendSharingProvider[];
  delegations: FrontendCredentialDelegation[];
  recipients: FrontendSharingRecipient[];
  actions: {
    create_url: string;
    remove_url: string;
  };
  limits: {
    providers: number;
    recipients: number;
    delegations: number;
  };
}

export type FrontendAssistantGuidancePromptId =
  | "chief_assistant"
  | "junior_assistant_mandate"
  | "direct_assistant_default";

export interface FrontendAssistantGuidancePrompt {
  id: FrontendAssistantGuidancePromptId;
  name: string;
  description: string;
  value: string;
  default_value: string;
  customized: boolean;
  truncated: boolean;
}

export interface FrontendAssistantGuidanceDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  summary: {
    organization_enabled: boolean;
    customized_prompts: number;
    total_prompts: number;
  };
  organization: {
    enabled: boolean;
    profile: string;
    truncated: boolean;
  };
  prompts: FrontendAssistantGuidancePrompt[];
  actions: {
    update_url: string;
  };
  limits: {
    organization_profile_chars: number;
    prompt_chars: number;
  };
}

export interface FrontendAssistantGuidanceUpdate {
  organization: {
    enabled: boolean;
    profile: string;
  };
  prompts: Record<FrontendAssistantGuidancePromptId, string>;
}

export interface FrontendAssistantGuidanceMutationResponse {
  success: boolean;
  message: string;
  guidance: FrontendAssistantGuidanceDashboard;
}

export type FrontendAssistantApprovalMode = "review" | "full_access";

export interface FrontendMemoryCategoryDefinition {
  key: string;
  label: string;
}

export interface FrontendAssistantPolicyDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  summary: {
    approval_mode: FrontendAssistantApprovalMode;
    tool_budget: number;
    user_hot_limit: number;
    archive_recall_default: boolean;
    configuration_needs_review: boolean;
  };
  runtime: {
    approval_mode: FrontendAssistantApprovalMode;
    max_tool_calls: number;
    effective_max_tool_calls: number;
    tool_budget_supported: boolean;
  };
  memory: {
    user_hot_limit: number;
    organization_hot_limit: number;
    include_archive_by_default: boolean;
    fallback_categories: FrontendMemoryCategoryDefinition[];
  };
  defaults: {
    runtime: {
      approval_mode: FrontendAssistantApprovalMode;
      max_tool_calls: number;
    };
    memory: {
      user_hot_limit: number;
      organization_hot_limit: number;
      include_archive_by_default: boolean;
    };
  };
  limits: {
    max_tool_calls: {
      minimum: number;
      maximum: number;
    };
    hot_memory: {
      minimum: number;
      maximum: number;
    };
  };
  actions: {
    update_url: string;
  };
}

export interface FrontendAssistantPolicyUpdate {
  runtime: {
    approval_mode: FrontendAssistantApprovalMode;
    max_tool_calls: number;
  };
  memory: {
    user_hot_limit: number;
    organization_hot_limit: number;
    include_archive_by_default: boolean;
  };
  acknowledge_full_access?: boolean;
}

export interface FrontendAssistantPolicyMutationResponse {
  success: boolean;
  message: string;
  policy: FrontendAssistantPolicyDashboard;
}

export type FrontendAdministrationView =
  | "overview"
  | "retrieval"
  | "continuity"
  | "members"
  | "usage";

export interface FrontendAdministrationGeneral {
  application_url: string;
  application_url_configured: boolean;
  site_policy: {
    markdown: string;
    published: boolean;
    preview_url: string;
    characters: number;
    truncated: boolean;
  };
  session_security: {
    source: "environment" | "database" | "temporary";
    label: string;
    persistent: boolean;
    recommended: boolean;
    browser_rotation_available: boolean;
    note: string;
  };
  limits: {
    policy_characters: number;
  };
}

export interface FrontendEmbeddingModelOption {
  id: string;
  name: string;
  provider: string;
  provider_label: string;
  supports_mrl: boolean;
  default_dimensions: number | null;
  prefix_style: string;
}

export interface FrontendRetrievalConfiguration {
  model_id: string;
  dimensions: number;
  task_type: string;
  top_k: number;
  chunk_size: number;
  chunk_overlap: number;
}

export interface FrontendRetrievalSettings {
  configuration: FrontendRetrievalConfiguration;
  models: FrontendEmbeddingModelOption[];
  index: {
    documents: number;
    chunks: number;
  };
  validation: {
    model_available: boolean;
    dimensions: boolean;
    task_type: boolean;
    top_k: boolean;
    chunk_size: boolean;
    chunk_overlap: boolean;
  };
  configuration_needs_review: boolean;
  task_types: Array<{
    id: string;
    label: string;
    description: string;
  }>;
  limits: Record<
    "dimensions" | "top_k" | "chunk_size" | "chunk_overlap",
    { minimum: number; maximum: number }
  >;
  actions: {
    update_url: string;
    preflight_url: string;
    rebuild_url: string;
  };
}

export interface FrontendBackupRecord {
  filename: string;
  size_bytes: number;
  created_at: string;
}

export interface FrontendContinuitySettings {
  database: {
    available: boolean;
    size_bytes: number;
  };
  backups: FrontendBackupRecord[];
  summary: {
    backup_count: number;
    latest_backup_at: string | null;
  };
  migration_policy: {
    plaintext_export_available: boolean;
    browser_reencryption_available: boolean;
    note: string;
  };
  actions: {
    create_backup_url: string;
    restore_backup_url: string;
  };
}

export interface FrontendMemberRecord {
  id: number;
  email: string;
  is_admin: boolean;
  is_current: boolean;
  can_delete: boolean;
  can_change_role: boolean;
}

export interface FrontendMemberDirectory {
  summary: {
    total: number;
    administrators: number;
    members: number;
    truncated: boolean;
  };
  members: FrontendMemberRecord[];
  limits: {
    members: number;
    email_characters: number;
    password_characters: {
      minimum: number;
      maximum: number;
    };
  };
  actions: {
    collection_url: string;
  };
}

export interface FrontendAdministrationDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  summary: {
    members: number;
    administrators: number;
    backups: number;
    rag_documents: number;
    rag_needs_review: boolean;
  };
  general: FrontendAdministrationGeneral;
  retrieval: FrontendRetrievalSettings;
  continuity: FrontendContinuitySettings;
  members: FrontendMemberDirectory;
  actions: {
    general_update_url: string;
    usage_url: string;
  };
}

export interface FrontendUsageDailyRecord {
  date: string;
  source: string;
  source_label: string;
  cost: number;
  tokens: number;
  requests: number;
}

export interface FrontendUsageMemberRecord {
  id: number;
  email: string;
  cost: number;
  tokens: number;
  requests: number;
}

export interface FrontendUsageDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  range: {
    start_date: string;
    end_date: string;
    days: number;
  };
  summary: {
    cost: number;
    tokens: number;
    requests: number;
    active_members: number;
  };
  daily: FrontendUsageDailyRecord[];
  by_user: FrontendUsageMemberRecord[];
  limits: {
    maximum_days: number;
    maximum_daily_rows: number;
    maximum_members: number;
  };
}

export interface FrontendAdministrationMutationResponse {
  success: boolean;
  message: string;
  general?: FrontendAdministrationGeneral;
  retrieval?: FrontendRetrievalSettings;
  continuity?: FrontendContinuitySettings;
  directory?: FrontendMemberDirectory;
  backup?: FrontendBackupRecord;
  requires_rebuild?: boolean;
  requires_restart?: boolean;
}

export interface FrontendRetrievalPreflightResponse {
  success: boolean;
  message: string;
  model_id: string;
  document_dimensions: number;
  query_dimensions: number;
  preflight_token: string;
  expires_in_seconds: number;
}

export interface FrontendSettingsMutationResponse {
  success?: boolean;
  message?: string;
  error?: string;
}

export interface FrontendOperationsLink {
  id: string;
  name: string;
  category: string;
  url: string;
}

export interface FrontendOperationsRunLink {
  id: string;
  title: string;
  url: string;
}

export interface FrontendOperationsApproval {
  id: string;
  title: string;
  summary: string;
  status: string;
  status_label: string;
  subject_label: string;
  tool_label: string | null;
  effects: string[];
  preview: unknown;
  resolution_note: unknown;
  created_at: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  can_approve: boolean;
  can_reject: boolean;
  can_cancel: boolean;
  resolve_url: string;
  run: FrontendOperationsRunLink | null;
  log_links: FrontendOperationsLink[];
}

export interface FrontendOperationsJob {
  id: string;
  title: string;
  summary: string;
  source_label: string;
  status: string;
  status_label: string;
  progress: number | null;
  active: boolean;
  needs_attention: boolean;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  run: FrontendOperationsRunLink | null;
  log_links: FrontendOperationsLink[];
}

export interface FrontendOperationsWorkerPid {
  value: number;
  terminate_url: string;
}

export interface FrontendOperationsWorker {
  id: string;
  name: string;
  status: string;
  status_label: string;
  configured: number;
  running: number;
  desired: number;
  healthy: boolean;
  entrypoint: string | null;
  environment: string;
  pids: FrontendOperationsWorkerPid[];
  stop_url: string;
  restart_url: string;
  log_link: FrontendOperationsLink | null;
}

export interface FrontendOperationsSchedule {
  id: string;
  job_id: string;
  title: string;
  addon_id: string;
  addon_name: string;
  status: string;
  status_label: string;
  trigger: string;
  next_run_at: string | null;
  paused: boolean;
  pending: boolean;
  pause_url: string;
  resume_url: string;
  trigger_url: string;
  log_link: FrontendOperationsLink | null;
}

export interface FrontendOperationsContainer {
  id: string;
  name: string;
  addon_id: string;
  service_name: string;
  legacy: boolean;
  status: string;
  status_label: string;
  image: string;
  created_at: string | null;
  action_url: string;
  log_links: FrontendOperationsLink[];
}

export interface FrontendOperationsDocker {
  available: boolean;
  status: string;
  message: string;
  containers: FrontendOperationsContainer[];
  log_links: FrontendOperationsLink[];
}

export interface FrontendOperationsSummary {
  pending_approvals: number;
  active_jobs: number;
  failed_jobs: number;
  attention_jobs: number;
  jobs_total: number;
  worker_processes: number;
  worker_addons: number;
  unhealthy_workers: number;
  schedules: number;
  containers: number;
  attention: number;
}

export interface FrontendOperationsDashboard {
  frontend_api_version: FrontendApiVersion;
  generated_at: string;
  permissions: {
    administer_system: boolean;
  };
  available_sections: string[];
  summary: FrontendOperationsSummary;
  counts: {
    approvals: Readonly<Record<string, number>>;
    jobs: Readonly<Record<string, number>>;
  };
  approvals: FrontendOperationsApproval[];
  jobs: FrontendOperationsJob[];
  workers: FrontendOperationsWorker[];
  schedules: FrontendOperationsSchedule[];
  docker: FrontendOperationsDocker | null;
  limits: {
    approvals: number;
    jobs: number;
  };
}

export interface FrontendOperationsActionResponse {
  success?: boolean;
  message?: string;
  status?: string;
}

export interface FrontendLogSource {
  id: string;
  name: string;
  category: string;
  description: string;
  exists: boolean;
  readable: boolean;
  writable: boolean;
  size: number;
  modified_at: string | null;
  warning: string | null;
  clearable: boolean;
  downloadable: boolean;
  max_bytes: number;
  backup_count: number;
  metadata: Readonly<Record<string, unknown>>;
  api_url: string;
}

export interface FrontendLogChunk {
  text: string;
  next_cursor: number;
  source: FrontendLogSource;
  warning: string | null;
}

export interface FrontendChatSummary {
  id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
}

export interface FrontendChatDirectory {
  items: FrontendChatSummary[];
  next_cursor: string | null;
}

export interface FrontendChatAttachment {
  id: string;
  name: string;
  url: string | null;
  size: number | null;
  media_type: string;
  strategy: string;
  source: string;
}

export interface FrontendAgentEvent {
  id?: string;
  kind: string;
  status?: string;
  title?: string;
  summary?: string;
  occurred_at?: string;
  sequence?: number;
  subject?: {
    type?: string;
    id?: string;
    [key: string]: unknown;
  };
  payload?: Readonly<Record<string, unknown>>;
  [key: string]: unknown;
}

interface FrontendChatTimelineBase {
  id: string;
  message_id: number;
  sequence: number;
  created_at: string | null;
}

export interface FrontendUserChatMessage extends FrontendChatTimelineBase {
  role: "user";
  text: string;
  attachments: FrontendChatAttachment[];
}

export interface FrontendAssistantChatMessage
  extends FrontendChatTimelineBase {
  role: "assistant";
  text: string;
  html: string;
  tts_audio_url: string | null;
  developer?: unknown;
}

export interface FrontendAgentEventMessage
  extends FrontendChatTimelineBase {
  role: "event";
  event: FrontendAgentEvent;
}

export interface FrontendDeveloperChatMessage
  extends FrontendChatTimelineBase {
  role: "developer";
  title: string;
  developer: unknown;
}

export type FrontendChatTimelineItem =
  | FrontendUserChatMessage
  | FrontendAssistantChatMessage
  | FrontendAgentEventMessage
  | FrontendDeveloperChatMessage;

export interface FrontendRuntimeInteraction {
  id: string;
  status: string;
  interaction_type: string;
  version?: number;
  tool_name?: string | null;
  approval_id?: string | null;
  request?: Readonly<Record<string, unknown>>;
  response?: Readonly<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface FrontendChatSession {
  id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  message_count: number;
  newest_message_id: number | null;
  revision: string;
  assistant_scope: {
    mode?: "default" | "direct" | string;
    capability_ids?: string[];
    [key: string]: unknown;
  };
  interactions: FrontendRuntimeInteraction[];
  stats: {
    cost: number;
    tokens: {
      prompt: number;
      completion: number;
      total: number;
    };
  };
}

export interface FrontendChatHistoryPage {
  session: FrontendChatSession;
  items: FrontendChatTimelineItem[];
  next_cursor: string | null;
  has_more: boolean;
  debug: boolean;
}

export type FrontendInspectorTab =
  | "overview"
  | "runs"
  | "memory"
  | "context"
  | "artifacts";

export interface FrontendRunProgress {
  completed: number;
  total: number;
  percent: number;
}

export interface FrontendRunSummary {
  id: string;
  title: string;
  summary: string;
  kind: string;
  status: string;
  active: boolean;
  needs_attention: boolean;
  current_step_label: string | null;
  progress: FrontendRunProgress;
  artifact_count: number;
  pending_approval_count: number;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  open_url: string;
}

export interface FrontendArtifactSummary {
  id: string;
  run_id: string | null;
  label: string;
  type: string;
  mime_type: string;
  size_bytes: number | null;
  review_status: string;
  preview_kind:
    | "image"
    | "audio"
    | "video"
    | "pdf"
    | "markdown"
    | "html"
    | "structured"
    | "text"
    | "download";
  version_number: number | null;
  source_addon_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  download_url: string | null;
}

export interface FrontendWorkspaceAttention {
  id: string;
  kind: string;
  title: string;
  summary: string;
  target_tab: FrontendInspectorTab;
}

export interface FrontendWorkspaceSummary {
  session: {
    id: string;
    title: string;
  };
  revision: string;
  updated_at: string | null;
  server_time: string;
  counts: {
    runs: number;
    active_runs: number;
    artifacts: number;
    artifact_reviews: number;
    session_memory: number;
    personal_memory: number;
    context_items: number;
    active_context_items: number;
    archived_context_items: number;
    excluded_context_items: number;
    attention: number;
  };
  focus_run: FrontendRunSummary | null;
  recent_runs: FrontendRunSummary[];
  recent_artifacts: FrontendArtifactSummary[];
  attention: FrontendWorkspaceAttention[];
}

export interface FrontendRunPage {
  items: FrontendRunSummary[];
  total: number;
  next_cursor: string | null;
}

export interface FrontendRunDirectoryItem extends FrontendRunSummary {
  status_label: string;
}

export interface FrontendRunDirectory {
  items: FrontendRunDirectoryItem[];
  total: number;
}

export interface FrontendRunControl {
  id: "start" | "pause" | "resume" | "cancel" | string;
  label: string;
  method: "POST";
  url: string;
  tone: "primary" | "neutral" | "danger" | string;
}

export interface FrontendRunCost {
  amount: number;
  currency: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  models: Array<{
    id: string;
    calls: number;
  }>;
}

export interface FrontendRunHeader {
  id: string;
  title: string;
  summary: string;
  kind: string;
  workflow_id: string | null;
  source_addon_id: string | null;
  status: string;
  status_label: string;
  needs_attention: boolean;
  progress: FrontendRunProgress & {
    failed: number;
    active: number;
  };
  current_step_id: string | null;
  current_step_label: string | null;
  created_at: string | null;
  started_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  cost: FrontendRunCost;
  controls: FrontendRunControl[];
  counts: {
    steps: number;
    artifacts: number;
    sources: number;
    evidence: number;
    approvals: number;
    pending_approvals: number;
    interactions: number;
  };
}

export interface FrontendRunStepSummary {
  id: string;
  key: string;
  title: string;
  purpose: string;
  type: string;
  status: string;
  status_label: string;
  sequence: number | null;
  role: string;
  parent_id: string | null;
  dependency_ids: string[];
  duration_ms: number | null;
  progress: number | null;
  needs_attention: boolean;
  artifact_count: number;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
}

export interface FrontendRunGraphEdge {
  id: string;
  source: string;
  target: string;
}

export interface FrontendRunGraph {
  nodes: FrontendRunStepSummary[];
  edges: FrontendRunGraphEdge[];
  large: boolean;
  truncated: boolean;
  layout_direction: "RIGHT" | "DOWN" | string;
}

export interface FrontendRunDocumentSection {
  id: string;
  title: string;
  summary: string;
  kind: string;
  value: unknown;
}

export interface FrontendRunDocument {
  summary: string;
  sections: FrontendRunDocumentSection[];
}

export interface FrontendRunSource {
  id: string;
  step_id: string | null;
  title: string;
  summary: string;
  type: string;
  provider: string | null;
  url: string | null;
  created_at: string | null;
}

export interface FrontendRunEvidence {
  id: string;
  step_id: string | null;
  artifact_id: string | null;
  claim: string;
  quote: string;
  citation: unknown;
  relevance_score: number | null;
  novelty_score: number | null;
  used_in_artifact: boolean;
  source: {
    id: string;
    title: string;
    url: string | null;
  } | null;
  created_at: string | null;
}

export interface FrontendRunApproval {
  id: string;
  status: string;
  subject_type: string;
  subject_id: string;
  tool_id: string | null;
  effects: string[];
  preview: unknown;
  resolution_note: unknown;
  created_at: string | null;
  expires_at: string | null;
  resolved_at: string | null;
  can_resolve: boolean;
  resolve_url: string;
}

export interface FrontendRunInteraction {
  id: string;
  status: string;
  type: string;
  tool_name: string | null;
  request: unknown;
  response: unknown;
  version: number;
  created_at: string | null;
  resolved_at: string | null;
  can_resolve: boolean;
  resolve_url: string;
}

export interface FrontendRunStudio {
  revision: string;
  server_time: string;
  run: FrontendRunHeader;
  documents: {
    understanding: FrontendRunDocument;
    plan: FrontendRunDocument;
    state: FrontendRunDocument;
  };
  graph: FrontendRunGraph;
  timeline: FrontendRunStepSummary[];
  artifacts: FrontendArtifactSummary[];
  sources: FrontendRunSource[];
  evidence: FrontendRunEvidence[];
  approvals: FrontendRunApproval[];
  interactions: FrontendRunInteraction[];
  selected_step_id: string | null;
  developer_url: string;
}

export interface FrontendRunField {
  key: string;
  label: string;
  description: string;
  type: string;
  format: string | null;
  required: boolean;
  summary: string;
  value: unknown;
}

export interface FrontendRunActivity {
  id: string;
  kind: string;
  title: string;
  summary: string;
  status: string;
  progress: number | null;
  occurred_at: string | null;
}

export interface FrontendRunStepDetail {
  step: FrontendRunStepSummary & {
    instructions: unknown;
    capabilities: string[];
    skills: string[];
    tools: string[];
    providers: unknown;
    can_rerun: boolean;
    rerun_url: string;
  };
  input: {
    summary: string;
    fields: FrontendRunField[];
    resources: Array<{
      id: string;
      label: string;
      type: string;
      provider: string | null;
      permissions: string[];
    }>;
    parent_outputs: Array<{
      step_id: string;
      title: string;
      summary: string;
      value: unknown;
    }>;
  };
  output: {
    summary: string;
    fields: FrontendRunField[];
    artifacts: FrontendArtifactSummary[];
  };
  activity: FrontendRunActivity[];
  problems: {
    has_problem: boolean;
    summary: string;
    guidance: string;
    log_links: Array<Readonly<Record<string, unknown>>>;
  };
  developer_url: string;
}

export interface FrontendMemoryEntry {
  id: string;
  scope: "session" | "personal";
  title: string;
  summary: string;
  value: unknown;
  value_kind:
    | "empty"
    | "boolean"
    | "number"
    | "structured"
    | "list"
    | "text";
  category: string;
  importance: string;
  tier: string | null;
  updated_at: string | null;
}

export interface FrontendMemoryPage {
  items: FrontendMemoryEntry[];
  total: number;
  next_cursor: string | null;
  scope: "all" | "session" | "personal";
  query: string;
}

export type FrontendContextLifecycleState =
  | "active"
  | "archived"
  | "excluded";

export type FrontendContextMaterialization =
  | "full"
  | "excerpt"
  | "summary"
  | "reference"
  | "overflow";

export interface FrontendContextItem {
  id: string;
  evidence_id: string;
  title: string;
  kind: string;
  lifecycle_state: FrontendContextLifecycleState;
  materialization: FrontendContextMaterialization;
  priority: number;
  is_pinned: boolean;
  token_estimate: number | null;
  char_count: number;
  byte_count: number;
  mime_type: string | null;
  content_hash: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface FrontendContextPage {
  items: FrontendContextItem[];
  total: number;
  next_cursor: string | null;
  state: "all" | FrontendContextLifecycleState;
  materialization: "all" | FrontendContextMaterialization;
  query: string;
  counts: {
    all: number;
    active: number;
    archived: number;
    excluded: number;
  };
  search_truncated: boolean;
}

export interface FrontendContextDetail {
  item: FrontendContextItem;
  content: string;
  start_offset: number;
  end_offset: number;
  has_more: boolean;
  next_offset: number | null;
  integrity_verified: boolean;
  evidence: {
    id: string;
    kind: string;
    role: string | null;
    mime_type: string | null;
    char_count: number;
    byte_count: number;
    token_estimate: number | null;
    content_hash: string;
  };
}

export interface FrontendContextUpdate {
  status: "updated";
  action: string;
  item: FrontendContextItem;
}

export interface FrontendArtifactPage {
  items: FrontendArtifactSummary[];
  total: number;
  next_cursor: string | null;
  search_truncated: boolean;
}

export interface FrontendArtifactVersion {
  id: string;
  version_number: number;
  label: string;
  type: string;
  mime_type: string;
  size_bytes: number | null;
  change_summary: unknown;
  source_count: number;
  evidence_count: number;
  created_at: string | null;
  current: boolean;
}

export interface FrontendArtifactSource {
  id: string;
  title: string;
  summary: string;
  type: string;
  provider: string | null;
  url: string | null;
  created_at: string | null;
}

export interface FrontendArtifactEvidence {
  id: string;
  claim: string;
  quote: string;
  citation: unknown;
  relevance_score: number | null;
  novelty_score: number | null;
  used_in_artifact: boolean;
  source: {
    id: string;
    title: string;
    url: string | null;
  } | null;
  created_at: string | null;
}

export interface FrontendArtifactDetail {
  artifact: FrontendArtifactSummary & {
    metadata: unknown;
    review: {
      status: string;
      note: unknown;
      reviewed_at: string | null;
      reviewed_by_user_id: number | null;
      can_review: boolean;
    };
    external_url: string | null;
    content_url: string;
  };
  versions: FrontendArtifactVersion[];
  versions_truncated: boolean;
  sources: FrontendArtifactSource[];
  evidence: FrontendArtifactEvidence[];
}

export interface FrontendArtifactContent {
  artifact: Readonly<Record<string, unknown>>;
  content: {
    available: boolean;
    content?: string;
    truncated?: boolean;
    rendered_html?: string;
    render_mode?: string;
    size_bytes?: number;
    error?: string;
    [key: string]: unknown;
  };
}

export interface FrontendChatModel {
  id: string;
  name: string;
  provider: string;
  category: string;
  supports_vision: boolean;
  supports_pdf: boolean;
  supports_audio_in: boolean;
  supports_reasoning?: boolean;
  supported_reasoning_levels?: string[];
  is_preset_fast?: boolean;
  is_preset_medium?: boolean;
  is_preset_powerful?: boolean;
}

export interface FrontendCapability {
  id: string;
  name?: string;
  title?: string;
  description?: string;
  group?: string;
  type?: string;
  capability_type?: string;
  [key: string]: unknown;
}

export interface FrontendInlineSearchContract {
  endpoint: string;
  min_query_length?: number;
  result_kind?: string;
}

export interface FrontendSlashCommand {
  command: string;
  description?: string;
  insert_text?: string;
  is_inline?: boolean;
  inline_search?: FrontendInlineSearchContract;
  is_prompt_passthrough?: boolean;
  helper_id?: string;
  ui_component_id?: string | null;
  workflow_id?: string | null;
  [key: string]: unknown;
}

export interface FrontendKnowledgeCollection {
  id: number;
  user_id: number;
  name: string;
  description: string | null;
  item_count: number;
  created_at: string | null;
  source_type: string;
  collection_mode: string;
  source_metadata: Readonly<Record<string, unknown>> | null;
  last_synced_at: string | null;
  storage_path: string | null;
}

export interface FrontendKnowledgeEngine {
  id: string;
  name: string;
  description: string;
}

export interface FrontendKnowledgeAccount {
  id: number;
  label: string;
  provider: string;
  active: boolean;
}

export interface FrontendKnowledgeOptions {
  engines: FrontendKnowledgeEngine[];
  email_accounts: FrontendKnowledgeAccount[];
  drive_accounts: FrontendKnowledgeAccount[];
  providers: {
    document_upload: boolean;
    email: boolean;
    googledrive: boolean;
  };
}

export interface FrontendKnowledgeSummary {
  total: number;
  vector: number;
  regular: number;
  items: number;
  source_types: Readonly<Record<string, number>>;
}

export interface FrontendKnowledgeDashboard {
  collections: FrontendKnowledgeCollection[];
  summary: FrontendKnowledgeSummary;
  options: FrontendKnowledgeOptions;
}

export interface FrontendKnowledgeItem {
  id: number | string;
  item_kind?: "legacy" | "canonical";
  canonical_source_id?: string;
  membership_id?: number;
  membership_metadata?: Readonly<Record<string, unknown>>;
  user_id: number;
  source_type: string;
  source_id: string | null;
  content_snippet: string | null;
  metadata: Readonly<Record<string, unknown>> | null;
  has_embedding: boolean;
  status?: string;
  current_version_number?: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface FrontendKnowledgeItemPage {
  items: FrontendKnowledgeItem[];
  total: number;
  pages: number;
  current_page: number;
  has_next: boolean;
  has_prev: boolean;
}

export interface FrontendKnowledgeChunk {
  id: number | string;
  text: string;
  meta: Readonly<Record<string, unknown>> | null;
}

export interface FrontendKnowledgeXray {
  id: number | string;
  item_kind?: "legacy" | "canonical";
  canonical_source_id?: string;
  source_type: string;
  source_id: string | null;
  metadata: Readonly<Record<string, unknown>> | null;
  raw_content: string;
  chunks: FrontendKnowledgeChunk[];
  chunk_total: number;
  chunk_pages: number;
  chunk_page: number;
  has_next: boolean;
  has_prev: boolean;
}

export interface FrontendKnowledgeLogLink {
  href: string;
  label?: string;
  name?: string;
  source_id?: string;
  category?: string;
}

export interface FrontendKnowledgeJobStatus {
  status?: string;
  message?: string;
  error?: string;
  progress?: number;
  log_links?: FrontendKnowledgeLogLink[];
  [key: string]: unknown;
}

export interface FrontendKnowledgeMutationResponse {
  success?: boolean;
  message?: string;
  item?: FrontendKnowledgeItem;
  job_id?: string;
  log_links?: FrontendKnowledgeLogLink[];
}

export interface FrontendKnowledgeStreamEvent {
  type: string;
  message: string;
  percent?: number;
  [key: string]: unknown;
}

export interface FrontendKnowledgeDriveItem {
  id: string;
  name: string;
  isFolder: boolean;
  mimeType: string;
  webViewLink?: string;
}

export interface FrontendKnowledgeDrivePickerResult
  extends FrontendKnowledgeDriveItem {
  items: FrontendKnowledgeDriveItem[];
  insert_text: string;
}

export interface FrontendAutomationStepDefinition {
  key: string;
  title: string;
  type: string;
  sequence?: number | null;
  depends_on: string[];
  assigned_model_role?: string | null;
  instructions?: string | null;
  capability_ids: string[];
  skill_ids: string[];
  tool_ids: string[];
  provider_requirements: string[];
  input_schema?: Readonly<Record<string, unknown>>;
  output_schema?: Readonly<Record<string, unknown>>;
  input?: Readonly<Record<string, unknown>>;
  metadata?: Readonly<Record<string, unknown>>;
}

export interface FrontendAutomationDefinition {
  version: number;
  name: string;
  description: string;
  steps: FrontendAutomationStepDefinition[];
}

export type FrontendAutomationTemplateScope =
  | "personal"
  | "shared"
  | "managed";

export interface FrontendAutomationTemplateSummary {
  id: string;
  title: string;
  description: string;
  owner_id: number | null;
  scope: FrontendAutomationTemplateScope;
  editable: boolean;
  is_active: boolean;
  active_version: number | null;
  version_count: number;
  step_count: number;
  definition_error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface FrontendAutomationTemplateVersion {
  version: number;
  name: string;
  description: string;
  step_count: number;
  created_at: string | null;
  created_by_user_id: number | null;
  change_summary: string;
  active: boolean;
}

export interface FrontendAutomationTemplate
  extends FrontendAutomationTemplateSummary {
  definition: FrontendAutomationDefinition;
  versions: FrontendAutomationTemplateVersion[];
}

export interface FrontendAutomationNamedRecord {
  id: string;
  label: string;
  type: string;
}

export interface FrontendAutomationLink {
  id: string;
  label: string;
  href: string | null;
  kind: "launch" | "state" | "log" | "resume" | string;
}

export interface FrontendAutomationWorkflow {
  id: string;
  name: string;
  description: string;
  addon_id: string;
  status: {
    state: string;
    reasons: string[];
  };
  available: boolean;
  dry_run_supported: boolean;
  execution_supported: boolean;
  definition_error: string | null;
  steps: FrontendAutomationStepDefinition[];
  requirements: {
    addons: string[];
    tools: string[];
    tool_views: string[];
    helpers: string[];
    providers: string[];
    services: string[];
    workers: string[];
    auth_scopes: string[];
    resource_scopes: string[];
  };
  approval_gates: string[];
  expected_jobs: FrontendAutomationNamedRecord[];
  expected_artifacts: FrontendAutomationNamedRecord[];
  links: FrontendAutomationLink[];
  launch_url: string | null;
}

export interface FrontendAutomationDashboard {
  templates: FrontendAutomationTemplateSummary[];
  workflows: FrontendAutomationWorkflow[];
  summary: {
    templates: number;
    personal_templates: number;
    addon_workflows: number;
    executable_workflows: number;
    attention: number;
  };
}

export interface FrontendAutomationRequirementItem {
  id: string;
  registered: boolean;
  state: string;
  ready: boolean;
  reasons: string[];
}

export interface FrontendAutomationRequirementGroup {
  id: string;
  label: string;
  total: number;
  ready: number;
  items: FrontendAutomationRequirementItem[];
}

export interface FrontendAutomationReadiness {
  workflow: FrontendAutomationWorkflow;
  available: boolean;
  can_dry_run: boolean;
  execution_supported: boolean;
  message: string;
  blocking_reasons: string[];
  step_count: number;
  requirements: FrontendAutomationRequirementGroup[];
  approval_gates: string[];
  expected_jobs: FrontendAutomationNamedRecord[];
  expected_artifacts: FrontendAutomationNamedRecord[];
  links: FrontendAutomationLink[];
}

export interface FrontendAutomationRunCreated {
  id: string;
  display_title: string;
  kind: string;
  workflow_id: string | null;
  status: string;
  step_ids: string[];
}

export interface FrontendApiClient {
  request<T>(
    path: string,
    init?: RequestInit & { signal?: AbortSignal },
  ): Promise<T>;
}

export interface MountedFrontend {
  unmount(): void | Promise<void>;
  updateCurrentPath?(currentPath: string): void | Promise<void>;
}

export interface FrontendUiComponentService {
  open<Input = unknown, Result = unknown>(
    componentId: string,
    input?: Input,
    options?: {
      api?: FrontendApiClient;
      document?: Document;
      fetchImplementation?: typeof fetch;
      overlayHost?: Element;
      signal?: AbortSignal;
    },
  ): Promise<Result | undefined>;
}

export interface FrontendMountContext {
  api: FrontendApiClient;
  apiVersion: FrontendApiVersion;
  basePath: string;
  signal: AbortSignal;
  featureFlags: FrontendFeatureMap;
}

export interface AddonMountContext extends FrontendMountContext {
  addonId: string;
  currentPath: string;
  navigate(
    href: string,
    options?: { replace?: boolean },
  ): "client" | "document";
  uiComponents: FrontendUiComponentService;
}

export interface UiComponentMountContext extends FrontendMountContext {
  addonId: string;
  componentId: string;
  input?: unknown;
  close(result?: unknown): void;
}

export interface VersionedFrontendModule<
  Context extends FrontendMountContext = FrontendMountContext,
> {
  apiVersion: FrontendApiVersion;
  mount(
    target: Element,
    context: Context,
  ): MountedFrontend | Promise<MountedFrontend>;
  unmount?(mounted: MountedFrontend): void | Promise<void>;
}

export type AddonApplicationModule = VersionedFrontendModule<AddonMountContext>;
export type UiComponentModule =
  VersionedFrontendModule<UiComponentMountContext>;

export interface FrontendModuleDescriptor {
  id: string;
  module_url: string;
  frontend_api_range: string;
}

export type AddonModuleDescriptor = FrontendModuleDescriptor;

export interface UiComponentModuleDescriptor
  extends FrontendModuleDescriptor {
  addon_id: string;
  input_schema_id: string | null;
  output_schema_id: string | null;
  input_schema: FrontendJsonSchema | null;
  output_schema: FrontendJsonSchema | null;
}
