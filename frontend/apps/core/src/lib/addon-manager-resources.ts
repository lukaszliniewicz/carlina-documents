import type {
  FrontendAddonCatalogResponse,
  FrontendAddonDoctorReport,
  FrontendAddonInventoryResponse,
  FrontendAddonManagerItem,
  FrontendAddonOperationListResponse,
  FrontendCapabilityResolutionReport,
  FrontendConnectionList,
} from "@carlina/sdk";
import { get, readonly, writable, type Readable } from "svelte/store";

import type { AddonManagerApi } from "./addon-manager-api.ts";

export type ResourceLoadResult<Value> =
  | { status: "success"; data: Value }
  | { status: "aborted" }
  | { status: "error"; error: string };

export interface ResourceSnapshot<Value> {
  data: Value | null;
  loading: boolean;
  loaded: boolean;
  error: string;
  requestedKey: string | null;
  resolvedKey: string | null;
}

export interface LatestResource<Arguments extends readonly unknown[], Value>
  extends Readable<ResourceSnapshot<Value>> {
  load(...args: Arguments): Promise<ResourceLoadResult<Value>>;
  reload(): Promise<ResourceLoadResult<Value>> | null;
  abort(): void;
  reset(): void;
  snapshot(): ResourceSnapshot<Value>;
}

interface LatestResourceOptions<Arguments extends readonly unknown[]> {
  key?: (...args: Arguments) => string;
  errorMessage: string;
}

function errorText(reason: unknown, fallback: string): string {
  return reason instanceof Error && reason.message.trim()
    ? reason.message
    : fallback;
}

export function createLatestResource<
  Arguments extends readonly unknown[],
  Value,
>(
  loader: (signal: AbortSignal, ...args: Arguments) => Promise<Value>,
  options: LatestResourceOptions<Arguments>,
): LatestResource<Arguments, Value> {
  const initial: ResourceSnapshot<Value> = {
    data: null,
    loading: false,
    loaded: false,
    error: "",
    requestedKey: null,
    resolvedKey: null,
  };
  const state = writable(initial);
  let generation = 0;
  let active:
    | {
        generation: number;
        key: string;
        controller: AbortController;
        promise: Promise<ResourceLoadResult<Value>>;
      }
    | null = null;
  let lastArguments: Arguments | null = null;

  function abort(): void {
    generation += 1;
    active?.controller.abort();
    active = null;
    state.update((current) => ({
      ...current,
      loading: false,
      requestedKey: null,
    }));
  }

  function load(...args: Arguments): Promise<ResourceLoadResult<Value>> {
    const key = options.key?.(...args) ?? "default";
    if (active?.key === key) return active.promise;

    active?.controller.abort();
    const controller = new AbortController();
    const requestGeneration = ++generation;
    lastArguments = args;
    state.update((current) => ({
      ...current,
      loading: true,
      error: "",
      requestedKey: key,
    }));

    const promise = (async (): Promise<ResourceLoadResult<Value>> => {
      try {
        const value = await loader(controller.signal, ...args);
        if (
          controller.signal.aborted ||
          requestGeneration !== generation ||
          active?.generation !== requestGeneration
        ) {
          return { status: "aborted" };
        }
        state.set({
          data: value,
          loading: false,
          loaded: true,
          error: "",
          requestedKey: null,
          resolvedKey: key,
        });
        return { status: "success", data: value };
      } catch (reason) {
        if (controller.signal.aborted || requestGeneration !== generation) {
          return { status: "aborted" };
        }
        const error = errorText(reason, options.errorMessage);
        state.update((current) => ({
          ...current,
          loading: false,
          error,
          requestedKey: null,
        }));
        return { status: "error", error };
      } finally {
        if (active?.generation === requestGeneration) active = null;
      }
    })();
    active = { generation: requestGeneration, key, controller, promise };
    return promise;
  }

  return {
    subscribe: readonly(state).subscribe,
    load,
    reload: () => (lastArguments ? load(...lastArguments) : null),
    abort,
    reset: () => {
      abort();
      lastArguments = null;
      state.set(initial);
    },
    snapshot: () => get(state),
  };
}

export interface AddonManagerResources {
  inventory: LatestResource<[], FrontendAddonInventoryResponse>;
  detail: LatestResource<[addonId: string], FrontendAddonManagerItem>;
  connections: LatestResource<[], FrontendConnectionList>;
  catalog: LatestResource<[], FrontendAddonCatalogResponse>;
  doctor: LatestResource<[], FrontendAddonDoctorReport>;
  capability: LatestResource<
    [addonId: string],
    FrontendCapabilityResolutionReport
  >;
  operations: LatestResource<[limit?: number], FrontendAddonOperationListResponse>;
  abortAll(): void;
}

export function createAddonManagerResources(
  api: AddonManagerApi,
): AddonManagerResources {
  const inventory = createLatestResource(
    (signal) => api.inventory(signal),
    { errorMessage: "The addon directory could not be loaded." },
  );
  const detail = createLatestResource(
    (signal, addonId: string) => api.detail(addonId, signal),
    {
      key: (addonId) => addonId,
      errorMessage: "Addon detail could not be loaded.",
    },
  );
  const connections = createLatestResource(
    (signal) => api.connections(signal),
    { errorMessage: "Addon connections could not be loaded." },
  );
  const catalog = createLatestResource(
    (signal) => api.catalog(signal),
    { errorMessage: "The first-party addon catalog could not be loaded." },
  );
  const doctor = createLatestResource(
    (signal) => api.doctor(signal),
    { errorMessage: "Addon diagnostics failed." },
  );
  const capability = createLatestResource(
    (signal, addonId: string) => api.capabilityResolution(addonId, signal),
    {
      key: (addonId) => addonId,
      errorMessage: "Capability resolution could not be loaded.",
    },
  );
  const operations = createLatestResource<
    [limit?: number],
    FrontendAddonOperationListResponse
  >(
    (signal, limit = 25) => api.operations(limit, signal),
    {
      key: (limit = 25) => String(limit),
      errorMessage: "Lifecycle operations could not be loaded.",
    },
  );
  const all = [
    inventory,
    detail,
    connections,
    catalog,
    doctor,
    capability,
    operations,
  ] as const;
  return {
    inventory,
    detail,
    connections,
    catalog,
    doctor,
    capability,
    operations,
    abortAll: () => all.forEach((resource) => resource.abort()),
  };
}
