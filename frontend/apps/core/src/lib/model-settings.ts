import type {
  FrontendCredentialDelegation,
  FrontendModelProvider,
  FrontendModelLimit,
  FrontendModelRecord,
  FrontendProviderConnection,
  FrontendSharingProvider,
} from "@carlina/sdk";

export type SettingsSection =
  | "profile"
  | "guidance"
  | "policy"
  | "models"
  | "routing"
  | "sharing"
  | "addons"
  | "administration";

export type ModelTypeFilter = "all" | "chat" | "embedding";
export type ModelStateFilter = "all" | "active" | "inactive";

export function optionalNumber(
  value: string | number | null | undefined,
): number | null {
  const normalized = String(value ?? "").trim();
  if (!normalized) return null;
  const parsed = Number(normalized);
  return Number.isFinite(parsed) ? parsed : null;
}

export function optionalInteger(
  value: string | number | null | undefined,
): number | null {
  const parsed = optionalNumber(value);
  return parsed !== null && Number.isInteger(parsed) && parsed > 0
    ? parsed
    : null;
}

export function modelLimit(
  model: FrontendModelRecord,
  kind: "input" | "output",
): FrontendModelLimit {
  const provided = model.limits?.[kind];
  if (provided) return provided;
  const detected = kind === "input" ? model.context_window : model.max_output_tokens ?? null;
  const known = detected !== null;
  const effective = detected ?? (kind === "input" ? 32_768 : 4_096);
  return {
    detected_tokens: detected,
    override_tokens: null,
    effective_tokens: effective,
    usable_tokens: kind === "input" ? Math.max(1, effective - Math.min(2_048, Math.max(256, Math.ceil(effective * 0.08)))) : null,
    known,
    source: known ? "db_model_record" : "conservative_fallback",
    source_label: known ? "Recorded model metadata" : "Provisional fallback (unknown)",
    confidence: known ? "high" : "low",
    provisional: !known,
  };
}

export function modelInputCapacityLabel(
  model: FrontendModelRecord,
): string {
  const limit = modelLimit(model, "input");
  const capacity = limit.effective_tokens.toLocaleString();
  return `${capacity} input tokens · ${limit.source_label}`;
}

export function settingsSectionFromRoute(route: string): SettingsSection {
  const pathname = new URL(route, "https://iarf.local").pathname.replace(
    /\/+$/,
    "",
  );
  if (pathname.startsWith("/settings/guidance")) return "guidance";
  if (pathname.startsWith("/settings/policy")) return "policy";
  if (pathname.startsWith("/settings/routing")) return "routing";
  if (pathname.startsWith("/settings/models")) return "models";
  if (pathname.startsWith("/settings/sharing")) return "sharing";
  if (pathname.startsWith("/settings/addons")) return "addons";
  if (pathname.startsWith("/settings/administration")) {
    return "administration";
  }
  return "profile";
}

/**
 * Maps the former provider-local routing view onto its app-level route.
 * The old desk never wrote this state itself, but accepting common tab query
 * names keeps externally shared links useful while the route settles.
 */
export function settingsRouteRedirect(route: string): string | null {
  const url = new URL(route, "https://iarf.local");
  const pathname = url.pathname.replace(/\/+$/, "");
  const legacyView = url.searchParams.get("view") ?? url.searchParams.get("tab");
  if (pathname === "/settings/models" && legacyView === "routing") {
    return "/settings/routing";
  }
  return null;
}

function normalized(value: unknown): string {
  return String(value ?? "").trim().toLocaleLowerCase();
}

function matchesEveryTerm(searchText: string, query: string): boolean {
  const terms = normalized(query).split(/\s+/).filter(Boolean);
  return terms.every((term) => searchText.includes(term));
}

export function providerSearchText(provider: FrontendModelProvider): string {
  return [
    provider.name,
    provider.key,
    provider.scope_label,
    provider.kind_label,
    provider.discovery_label,
    provider.status_label,
  ]
    .map(normalized)
    .join(" ");
}

export function filterModelProviders(
  providers: FrontendModelProvider[],
  query: string,
): FrontendModelProvider[] {
  const needle = normalized(query);
  if (!needle) return providers;
  return providers.filter((provider) =>
    matchesEveryTerm(providerSearchText(provider), needle),
  );
}

export function modelSearchText(model: FrontendModelRecord): string {
  return [
    model.name,
    model.id,
    model.provider_name,
    model.scope_label,
    model.type,
    model.active ? "active enabled" : "inactive disabled",
    model.capabilities.vision ? "vision images" : "",
    model.capabilities.pdf ? "pdf documents" : "",
    model.capabilities.reasoning ? "reasoning thinking" : "",
    model.capabilities.audio ? "audio" : "",
    modelLimit(model, "input").known ? "known capacity" : "unknown capacity provisional",
    modelLimit(model, "input").source_label,
    model.embedding.supports_mrl ? "matryoshka mrl" : "",
  ]
    .map(normalized)
    .join(" ");
}

export function filterModels(
  models: FrontendModelRecord[],
  options: {
    query: string;
    type: ModelTypeFilter;
    state: ModelStateFilter;
  },
): FrontendModelRecord[] {
  const needle = normalized(options.query);
  return models.filter((model) => {
    if (options.type !== "all" && model.type !== options.type) return false;
    if (options.state === "active" && !model.active) return false;
    if (options.state === "inactive" && model.active) return false;
    return !needle || matchesEveryTerm(modelSearchText(model), needle);
  });
}

export function providerTone(
  provider: FrontendModelProvider,
): "success" | "warning" | "neutral" {
  if (provider.status === "ready") return "success";
  if (provider.status === "attention") return "warning";
  return "neutral";
}

export function connectionTone(
  connection: FrontendProviderConnection,
): "success" | "neutral" {
  return connection.active ? "success" : "neutral";
}

export function formatSettingsDate(value: string | null): string {
  if (!value) return "Not recorded";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
  }).format(parsed);
}

export function imageDetailOptions(
  model: FrontendModelRecord,
): Array<{ value: string; label: string }> {
  const base = [
    { value: "preserve", label: "Provider default" },
    { value: "omit", label: "Omit detail hint" },
    { value: "auto", label: "Automatic" },
    { value: "low", label: "Low detail" },
  ];
  const family = normalized(
    `${model.id} ${model.name} ${model.provider_name}`,
  );
  if (
    family.includes("gemini") ||
    family.includes("gemma") ||
    family.includes("vertex") ||
    family.includes("google")
  ) {
    return [
      ...base,
      { value: "medium", label: "Medium detail" },
      { value: "high", label: "High detail" },
      { value: "ultra_high", label: "Ultra-high detail" },
    ];
  }
  return [...base, { value: "high", label: "High detail" }];
}

export function availableSharingCredentials(
  providers: FrontendSharingProvider[],
): number {
  return providers.reduce(
    (total, provider) => total + provider.credentials.length,
    0,
  );
}

export function delegationSearchText(
  delegation: FrontendCredentialDelegation,
): string {
  return [
    delegation.provider_name,
    delegation.provider_id,
    delegation.credential_name,
    delegation.delegatee_email,
  ]
    .map(normalized)
    .join(" ");
}

export function filterDelegations(
  delegations: FrontendCredentialDelegation[],
  query: string,
): FrontendCredentialDelegation[] {
  const needle = normalized(query);
  if (!needle) return delegations;
  return delegations.filter((delegation) =>
    matchesEveryTerm(delegationSearchText(delegation), needle),
  );
}
