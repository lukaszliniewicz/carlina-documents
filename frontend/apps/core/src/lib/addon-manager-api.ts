import type {
  FrontendAddonActionResponse,
  FrontendAddonCatalogResponse,
  FrontendAddonDependencyNotice,
  FrontendAddonDoctorReport,
  FrontendAddonInventoryResponse,
  FrontendAddonManagerItem,
  FrontendAddonOperation,
  FrontendAddonOperationListResponse,
  FrontendApiClient,
  FrontendCapabilityCandidate,
  FrontendCapabilityResolutionReport,
  FrontendConnectionList,
} from "@carlina/sdk";

export type AddonLifecycleCommand =
  | {
      kind: "activate";
      addonId: string;
      dependencyReview: readonly string[];
    }
  | { kind: "deactivate" | "uninstall"; addonId: string }
  | { kind: "purge"; addonId: string; confirmation: string }
  | { kind: "stop-workers" | "restart-workers"; addonId: string }
  | {
      kind: "pause-job" | "resume-job" | "run-job";
      addonId: string;
      jobId: string;
    }
  | { kind: "restart-server" };

export interface CapabilityBindingInput {
  requirementId: string;
  candidate: FrontendCapabilityCandidate;
  connectionId?: string;
  selected: boolean;
}

export interface CatalogInstallInput {
  addonId: string;
  version: string;
  catalogSha256: string;
  archiveSha256: string;
  approveNativeCode: boolean;
  dependencyReview: readonly string[];
}

export interface LocalInstallInput {
  file: File;
  approveNativeCode: boolean;
  dependencyReview: readonly string[];
}

export interface AddonManagerApi {
  inventory(signal?: AbortSignal): Promise<FrontendAddonInventoryResponse>;
  detail(addonId: string, signal?: AbortSignal): Promise<FrontendAddonManagerItem>;
  connections(signal?: AbortSignal): Promise<FrontendConnectionList>;
  catalog(signal?: AbortSignal): Promise<FrontendAddonCatalogResponse>;
  doctor(signal?: AbortSignal): Promise<FrontendAddonDoctorReport>;
  capabilityResolution(
    addonId: string,
    signal?: AbortSignal,
  ): Promise<FrontendCapabilityResolutionReport>;
  operations(
    limit?: number,
    signal?: AbortSignal,
  ): Promise<FrontendAddonOperationListResponse>;
  refreshDiscovery(): Promise<FrontendAddonActionResponse>;
  bindCapability(
    addonId: string,
    input: CapabilityBindingInput,
  ): Promise<void>;
  controlOperation(
    operation: FrontendAddonOperation,
    action: "cancel" | "retry",
  ): Promise<FrontendAddonOperation>;
  execute(command: AddonLifecycleCommand): Promise<FrontendAddonActionResponse>;
  installLocal(input: LocalInstallInput): Promise<FrontendAddonActionResponse>;
  installCatalog(
    input: CatalogInstallInput,
  ): Promise<FrontendAddonActionResponse>;
}

interface ApiRequestError extends Error {
  status?: number;
  payload?: unknown;
}

interface DependencyReviewPayload {
  status?: unknown;
  dependency_notices?: unknown;
}

type FrontendRequestInit = RequestInit & { signal?: AbortSignal };

const JOB_ACTIONS = {
  "pause-job": "pause",
  "resume-job": "resume",
  "run-job": "trigger",
} as const;

function addonPath(addonId: string, suffix: string): string {
  return `/api/addons/${encodeURIComponent(addonId)}${suffix}`;
}

function jsonRequest(method: string, body?: unknown): FrontendRequestInit {
  return {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

function lifecycleRequest(command: AddonLifecycleCommand): {
  path: string;
  init: FrontendRequestInit;
} {
  if (command.kind === "restart-server") {
    return { path: "/api/system/restart", init: jsonRequest("POST") };
  }
  if (command.kind === "stop-workers" || command.kind === "restart-workers") {
    const action = command.kind === "stop-workers" ? "stop" : "restart";
    return {
      path: addonPath(command.addonId, `/workers/${action}`),
      init: jsonRequest("POST"),
    };
  }
  if (
    command.kind === "pause-job" ||
    command.kind === "resume-job" ||
    command.kind === "run-job"
  ) {
    const jobCommand = command;
    return {
      path: addonPath(
        jobCommand.addonId,
        `/jobs/${encodeURIComponent(jobCommand.jobId)}/${JOB_ACTIONS[jobCommand.kind]}`,
      ),
      init: jsonRequest("POST"),
    };
  }

  const path = {
    activate: "/api/addons/activate",
    deactivate: "/api/addons/deactivate",
    uninstall: "/api/addons/uninstall",
    purge: "/api/addons/purge-data",
  }[command.kind];
  const body =
    command.kind === "activate"
      ? {
          addon_id: command.addonId,
          dependency_review: [...command.dependencyReview],
        }
      : command.kind === "purge"
        ? { addon_id: command.addonId, confirmation: command.confirmation }
        : { addon_id: command.addonId };
  return { path, init: jsonRequest("POST", body) };
}

function isDependencyNotice(value: unknown): value is FrontendAddonDependencyNotice {
  if (!value || typeof value !== "object") return false;
  const notice = value as Record<string, unknown>;
  return (
    typeof notice.id === "string" &&
    Array.isArray(notice.packages) &&
    notice.packages.every((packageName) => typeof packageName === "string") &&
    typeof notice.license === "string" &&
    typeof notice.purpose === "string" &&
    typeof notice.review_required === "boolean"
  );
}

export function addonApiErrorStatus(error: unknown): number | undefined {
  return error instanceof Error
    ? (error as ApiRequestError).status
    : undefined;
}

export function addonApiErrorPayload(
  error: unknown,
): Readonly<DependencyReviewPayload> | null {
  if (!(error instanceof Error)) return null;
  const payload = (error as ApiRequestError).payload;
  return payload && typeof payload === "object"
    ? (payload as DependencyReviewPayload)
    : null;
}

export function dependencyReviewNoticesFromError(
  error: unknown,
): FrontendAddonDependencyNotice[] {
  const notices = addonApiErrorPayload(error)?.dependency_notices;
  return Array.isArray(notices) ? notices.filter(isDependencyNotice) : [];
}

export function createAddonManagerApi(client: FrontendApiClient): AddonManagerApi {
  return {
    inventory: (signal) =>
      client.request<FrontendAddonInventoryResponse>("/api/addons/inventory", {
        signal,
      }),
    detail: (addonId, signal) =>
      client.request<FrontendAddonManagerItem>(
        addonPath(addonId, "/detail"),
        { signal },
      ),
    connections: (signal) =>
      client.request<FrontendConnectionList>("/api/addon-connections", {
        signal,
      }),
    catalog: (signal) =>
      client.request<FrontendAddonCatalogResponse>("/api/addons/catalog", {
        signal,
      }),
    doctor: (signal) =>
      client.request<FrontendAddonDoctorReport>("/api/addons/doctor", {
        signal,
      }),
    capabilityResolution: (addonId, signal) =>
      client.request<FrontendCapabilityResolutionReport>(
        addonPath(addonId, "/capability-resolution"),
        { signal },
      ),
    operations: (limit = 25, signal) =>
      client.request<FrontendAddonOperationListResponse>(
        `/api/addons/operations?limit=${encodeURIComponent(String(limit))}`,
        { signal },
      ),
    refreshDiscovery: () =>
      client.request<FrontendAddonActionResponse>(
        "/api/addons/refresh",
        jsonRequest("POST"),
      ),
    bindCapability: (addonId, input) =>
      client.request<void>(
        addonPath(addonId, "/capability-bindings"),
        jsonRequest("PUT", {
          requirement_id: input.requirementId,
          provider_addon_id: input.candidate.addon_id,
          provider_capability_id: input.candidate.provider_id,
          connection_id: input.connectionId,
          selected: input.selected,
        }),
      ),
    controlOperation: (operation, action) =>
      client.request<FrontendAddonOperation>(
        action === "cancel" ? operation.cancel_url : operation.retry_url,
        jsonRequest("POST"),
      ),
    execute: (command) => {
      const request = lifecycleRequest(command);
      return client.request<FrontendAddonActionResponse>(request.path, request.init);
    },
    installLocal: (input) => {
      const form = new FormData();
      form.append("addon_zip", input.file, input.file.name);
      form.append("approve_native_code", String(input.approveNativeCode));
      for (const noticeId of input.dependencyReview) {
        form.append("dependency_review", noticeId);
      }
      return client.request<FrontendAddonActionResponse>("/api/addons/install", {
        method: "POST",
        body: form,
      });
    },
    installCatalog: (input) =>
      client.request<FrontendAddonActionResponse>(
        "/api/addons/catalog/install",
        jsonRequest("POST", {
          addon_id: input.addonId,
          version: input.version,
          catalog_sha256: input.catalogSha256,
          archive_sha256: input.archiveSha256,
          approve_native_code: input.approveNativeCode,
          dependency_review: [...input.dependencyReview],
        }),
      ),
  };
}
