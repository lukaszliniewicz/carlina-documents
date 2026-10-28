import type {
  FrontendAddonManagerItem,
  FrontendAddonManagerSummary,
  FrontendAddonOperation,
} from "@carlina/sdk";

export type FrontendAddonDirectoryItem =
  | FrontendAddonManagerItem
  | FrontendAddonManagerSummary;

export type AddonDirectoryStatus =
  | "all"
  | "active"
  | "inactive"
  | "attention";

export interface AddonDirectoryFilters {
  query: string;
  status: AddonDirectoryStatus;
  role: string;
  category: string;
  kind?: string;
}

export interface AddonDirectorySummary {
  total: number;
  active: number;
  inactive: number;
  attention: number;
  runningWorkers: number;
}

export interface AddonActionConfirmation {
  title: string;
  description: string;
  confirmLabel: string;
  danger?: boolean;
  requiresIdentifier?: boolean;
  confirmationPhrase?: string;
}

const ACTIVE_OPERATION_STATUSES = new Set([
  "queued",
  "running",
  "waiting_for_approval",
  "paused",
]);

export function addonOperationActive(
  operation: FrontendAddonOperation,
): boolean {
  return ACTIVE_OPERATION_STATUSES.has(operation.status.toLocaleLowerCase());
}

export function addonOperationProgress(
  operation: FrontendAddonOperation,
): number {
  const value = Number(operation.progress ?? 0);
  return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
}

export function addonOperationStatusLabel(
  operation: FrontendAddonOperation,
): string {
  if (operation.cancel_requested && addonOperationActive(operation)) {
    return "Cancelling";
  }
  const status = operation.status.toLocaleLowerCase();
  if (status === "queued") return "Queued";
  if (status === "running") return "In progress";
  if (status === "completed") return "Completed";
  if (status === "failed") return "Failed";
  if (status === "cancelled") return "Cancelled";
  return status.replaceAll("_", " ") || "Unknown";
}

export function addonOperationTone(
  operation: FrontendAddonOperation,
): "neutral" | "success" | "warning" | "danger" {
  const status = operation.status.toLocaleLowerCase();
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "cancelled" || operation.cancel_requested) return "warning";
  return "neutral";
}

function normalizedValues(values: readonly string[] | undefined): string[] {
  return (values ?? [])
    .map((value) => String(value).trim())
    .filter(Boolean);
}

export function addonNeedsAttention(addon: FrontendAddonDirectoryItem): boolean {
  const packageState = String(addon.status?.state ?? "").toLowerCase();
  const buildState = String(
    "frontend_status" in addon
      ? addon.frontend_status
      : addon.frontend_build?.status,
  ).toLowerCase();
  const rebuildNeeded =
    "frontend_rebuild_needed" in addon
      ? addon.frontend_rebuild_needed
      : Boolean(addon.frontend_build?.rebuild_needed);
  const lifecycleState = String(addon.lifecycle_state ?? "").toLowerCase();
  return (
    ["degraded", "failed", "missing_dependency", "misconfigured"].includes(
      packageState,
    ) ||
    ["degraded", "failed"].includes(lifecycleState) ||
    Boolean(addon.is_active && addon.builtin_reconciliation_required) ||
    rebuildNeeded ||
    ["failed", "missing", "misconfigured", "stale"].includes(buildState)
  );
}

export function addonSearchText(addon: FrontendAddonDirectoryItem): string {
  const capabilityRequirements =
    "capability_requirements" in addon
      ? (addon.capability_requirements ?? [])
      : [];
  const capabilityProviders =
    "capability_providers" in addon
      ? (addon.capability_providers ?? [])
      : [];
  const dependencyNotices =
    "dependency_notices" in addon ? (addon.dependency_notices ?? []) : [];
  return [
    addon.name,
    addon.id,
    addon.description,
    addon.author,
    addon.kind ?? "",
    addon.distribution?.channel ?? "",
    addon.distribution?.publisher ?? "",
    ...normalizedValues(addon.roles),
    ...normalizedValues(addon.categories),
    ...normalizedValues(addon.provider_domains),
    ...normalizedValues(addon.dependencies),
    ...capabilityRequirements.flatMap((requirement) => [
      requirement.id,
      requirement.capability,
      ...normalizedValues(requirement.operations),
    ]),
    ...capabilityProviders.flatMap((provider) => [
      provider.id,
      provider.capability,
      ...normalizedValues(provider.operations),
    ]),
    ...dependencyNotices.flatMap((notice) => [
      notice.license,
      notice.purpose,
      ...normalizedValues(notice.packages),
    ]),
    ...("search_terms" in addon ? normalizedValues(addon.search_terms) : []),
    addon.inferred_hub?.hub_label ?? "",
  ]
    .join(" ")
    .toLocaleLowerCase();
}

export function filterAddonDirectory<T extends FrontendAddonDirectoryItem>(
  addons: readonly T[],
  filters: AddonDirectoryFilters,
): T[] {
  const query = filters.query.trim().toLocaleLowerCase();
  return addons.filter((addon) => {
    if (filters.status === "active" && !addon.is_active) return false;
    if (filters.status === "inactive" && addon.is_active) return false;
    if (filters.status === "attention" && !addonNeedsAttention(addon)) {
      return false;
    }
    if (
      filters.role !== "all" &&
      !normalizedValues(addon.roles).includes(filters.role)
    ) {
      return false;
    }
    if (
      filters.category !== "all" &&
      !normalizedValues(addon.categories).includes(filters.category)
    ) {
      return false;
    }
    if (
      filters.kind &&
      filters.kind !== "all" &&
      addon.kind !== filters.kind
    ) {
      return false;
    }
    return !query || addonSearchText(addon).includes(query);
  });
}

export function summarizeAddonDirectory(
  addons: readonly FrontendAddonDirectoryItem[],
): AddonDirectorySummary {
  return addons.reduce<AddonDirectorySummary>(
    (summary, addon) => {
      if (addon.is_active) summary.active += 1;
      else summary.inactive += 1;
      if (addonNeedsAttention(addon)) summary.attention += 1;
      summary.runningWorkers += Number(
        "worker_running" in addon
          ? addon.worker_running
          : addon.worker_state?.running ?? 0,
      );
      return summary;
    },
    {
      total: addons.length,
      active: 0,
      inactive: 0,
      attention: 0,
      runningWorkers: 0,
    },
  );
}

export function collectAddonFacets(
  addons: readonly FrontendAddonDirectoryItem[],
  key: "roles" | "categories",
): string[] {
  return Array.from(
    new Set(addons.flatMap((addon) => normalizedValues(addon[key]))),
  ).sort((left, right) => left.localeCompare(right));
}

export function collectAddonKinds(
  addons: readonly FrontendAddonDirectoryItem[],
): string[] {
  return Array.from(
    new Set(addons.map((addon) => addon.kind).filter((kind): kind is string => Boolean(kind))),
  ).sort((left, right) => left.localeCompare(right));
}

export function addonStatusLabel(addon: FrontendAddonDirectoryItem): string {
  if (addon.lifecycle_state === "data_purged") return "Data purged";
  if (addon.code_available === false) return "Code removed";
  if (!addon.is_active) return "Inactive";
  if (addonNeedsAttention(addon)) return "Needs attention";
  return "Active";
}

export function addonStatusTone(
  addon: FrontendAddonDirectoryItem,
): "neutral" | "success" | "warning" | "danger" {
  if (!addon.is_active) return "neutral";
  return addonNeedsAttention(addon) ? "warning" : "success";
}

export function addonBuildTone(
  status: string | undefined,
): "neutral" | "success" | "warning" | "danger" {
  const normalized = String(status ?? "").toLocaleLowerCase();
  if (normalized === "built") return "success";
  if (["stale", "missing"].includes(normalized)) return "warning";
  if (["failed", "misconfigured"].includes(normalized)) return "danger";
  return "neutral";
}

export function readableAddonLabel(value: string | null | undefined): string {
  const normalized = String(value ?? "").trim();
  if (!normalized) return "Not declared";
  return normalized.replaceAll("_", " ");
}

export function addonDateLabel(value: string | null | undefined): string {
  if (!value) return "Not scheduled";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

export function addonByteLabel(value: number | null | undefined): string {
  if (!Number.isFinite(value) || Number(value) < 0) return "Unknown size";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1_048_576) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1_073_741_824) {
    return `${(bytes / 1_048_576).toFixed(1)} MB`;
  }
  return `${(bytes / 1_073_741_824).toFixed(2)} GB`;
}

export function addonOperationLabel(operation: FrontendAddonOperation): string {
  if (operation.operation === "activate") return "Activation";
  if (operation.operation === "install_catalog") return "Catalog installation";
  if (operation.operation === "install_upload") {
    return "Local package installation";
  }
  return readableAddonLabel(operation.operation);
}

export function addonReadinessEntries(
  operation: FrontendAddonOperation,
): Array<{ label: string; value: string }> {
  return Object.entries(operation.readiness ?? {}).map(([label, value]) => {
    let rendered = String(value ?? "Not reported");
    if (value && typeof value === "object") {
      const status = (value as Readonly<Record<string, unknown>>).status;
      rendered =
        typeof status === "string" ? status : JSON.stringify(value).slice(0, 180);
    }
    return {
      label: readableAddonLabel(label),
      value: readableAddonLabel(rendered),
    };
  });
}

export function addonCatalogStatusLabel(value: string): string {
  if (value === "update_available") return "Update available";
  if (value === "installed") return "Installed";
  if (value === "available") return "Available";
  if (value === "incompatible") return "Not compatible";
  return readableAddonLabel(value);
}

export function addonCatalogRecordLabel(
  value: Readonly<Record<string, unknown>>,
): string {
  const id = typeof value.id === "string" ? value.id : "contract entry";
  const detail = [value.mode, value.license, value.access]
    .flatMap((item) => (Array.isArray(item) ? item : [item]))
    .filter((item): item is string => typeof item === "string" && Boolean(item));
  return detail.length ? `${id} (${detail.join(", ")})` : id;
}

export function addonEffectiveCodeAccess(
  addon: FrontendAddonManagerItem,
): string {
  if (addon.effective_code_access) return addon.effective_code_access;
  if (
    ["built_in_trusted", "installed_trusted_code"].includes(
      addon.trust_tier ?? "",
    )
  ) {
    return "host_process";
  }
  if (addon.trust_tier === "isolated_service") return "container_service";
  if (addon.trust_tier === "remote_integration") return "remote_service";
  return "unclassified";
}

export function addonCodeAccessLabel(value: string): string {
  if (value === "host_process") return "Full host-process access";
  if (value === "host_user_process") return "Host-user child process";
  if (value === "container_service") return "Container service boundary";
  if (value === "remote_service") return "Remote service";
  if (value === "declarative_only") return "Declarative only";
  return "Not yet classified";
}
