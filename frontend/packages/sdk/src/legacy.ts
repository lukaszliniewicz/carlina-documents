import {
  FRONTEND_API_VERSION,
  type FrontendModuleDescriptor,
  type VersionedFrontendModule,
} from "./contracts.ts";
import {
  loadVersionedFrontendModule,
  resolveSameOriginModuleUrl,
  type ModuleImporter,
} from "./loader.ts";

export interface LegacyIarfFrontendBridge {
  apiVersion: typeof FRONTEND_API_VERSION;
  loadModule(
    descriptor: FrontendModuleDescriptor,
  ): Promise<VersionedFrontendModule>;
  resolveModuleUrl(rawUrl: string): string;
}

export interface CarlinaFrontendNamespace {
  frontend?: LegacyIarfFrontendBridge;
  loadUiComponent?: (
    componentId: string,
    options?: Record<string, unknown>,
  ) => Promise<unknown>;
  loadHelper?: (
    componentId: string,
    options?: Record<string, unknown>,
  ) => Promise<unknown>;
  [key: string]: unknown;
}

declare global {
  interface Window {
    CARLINA?: CarlinaFrontendNamespace;
    IARF_AI?: CarlinaFrontendNamespace;
  }
}

/** @deprecated Use {@link CarlinaFrontendNamespace}. */
export type LegacyIarfNamespace = CarlinaFrontendNamespace;

export function installLegacyFrontendAdapter(
  targetWindow: Window = window,
  importModule?: ModuleImporter,
): CarlinaFrontendNamespace {
  const namespace = targetWindow.CARLINA ?? targetWindow.IARF_AI ?? {};
  const origin = targetWindow.location.origin;
  namespace.frontend = {
    apiVersion: FRONTEND_API_VERSION,
    loadModule: (descriptor) =>
      loadVersionedFrontendModule(
        descriptor,
        origin,
        importModule,
      ),
    resolveModuleUrl: (rawUrl) =>
      resolveSameOriginModuleUrl(rawUrl, origin),
  };

  // Existing pages own the legacy loader. Preserve it verbatim while exposing
  // the versioned bridge beside it; pages without that script get a narrow
  // adapter that accepts an explicit descriptor.
  if (typeof namespace.loadUiComponent !== "function") {
    namespace.loadUiComponent = async (
      componentId,
      options = {},
    ): Promise<unknown> => {
      const descriptor = options.descriptor;
      if (
        !descriptor ||
        typeof descriptor !== "object" ||
        !("module_url" in descriptor) ||
        !("frontend_api_range" in descriptor)
      ) {
        return null;
      }
      return namespace.frontend?.loadModule({
        id: componentId,
        module_url: String(descriptor.module_url),
        frontend_api_range: String(descriptor.frontend_api_range),
      });
    };
  }
  if (typeof namespace.loadHelper !== "function") {
    namespace.loadHelper = namespace.loadUiComponent;
  }

  targetWindow.CARLINA = namespace;
  targetWindow.IARF_AI = namespace;
  return namespace;
}
