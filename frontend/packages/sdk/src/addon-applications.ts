import { createAddonApiClient } from "./api.ts";
import {
  FRONTEND_API_VERSION,
  type AddonApplicationModule,
  type AddonMountContext,
  type FrontendAddonDescriptor,
  type FrontendBootstrap,
  type MountedFrontend,
} from "./contracts.ts";
import {
  loadVersionedFrontendModule,
  type ModuleImporter,
} from "./loader.ts";
import {
  loadFrontendStylesheets,
  type FrontendStylesheetLoader,
} from "./stylesheets.ts";
import { createUiComponentService } from "./ui-components.ts";

const ADDON_TARGET_ATTRIBUTE = "data-carlina-addon-target";
const LEGACY_ADDON_TARGET_ATTRIBUTE = "data-iarf-addon-target";

export interface AddonApplicationMatch {
  addon: FrontendAddonDescriptor;
  basePath: string;
}

export function addonApplicationMountKey(
  match: AddonApplicationMatch | undefined,
  bootstrap: FrontendBootstrap,
): string {
  if (!match) return "";
  const { addon, basePath } = match;
  return JSON.stringify({
    id: addon.id,
    version: addon.version,
    basePath,
    moduleUrl: addon.module_url,
    frontendApiRange: addon.frontend_api_range,
    cssUrls: addon.css_urls,
    build: addon.build,
    enabled: bootstrap.feature_flags[`frontend.addon.${addon.id}`],
    capabilities: bootstrap.capabilities,
    uiComponents: bootstrap.ui_components,
  });
}

export interface AddonApplicationMountOptions {
  applicationOrigin?: string;
  document?: Document;
  fetchImplementation?: typeof fetch;
  importModule?: ModuleImporter;
  loadStylesheets?: FrontendStylesheetLoader;
  signal?: AbortSignal;
  navigate(
    href: string,
    options?: { replace?: boolean },
  ): "client" | "document";
}

function normalizedPath(rawPath: string): string {
  const pathname = new URL(rawPath, "https://carlina.local").pathname;
  return pathname.replace(/\/+$/, "") || "/";
}

function routeMatches(pathname: string, route: string): boolean {
  const normalizedRoute = normalizedPath(route);
  return (
    normalizedRoute === pathname ||
    (normalizedRoute !== "/" && pathname.startsWith(`${normalizedRoute}/`))
  );
}

export function addonApplicationEnabled(
  addon: FrontendAddonDescriptor,
  bootstrap: Pick<FrontendBootstrap, "feature_flags">,
): boolean {
  return Boolean(
    addon.active &&
      addon.compatible &&
      addon.module_url &&
      addon.frontend_api_range &&
      addon.build.status === "built" &&
      !addon.build.rebuild_needed &&
      bootstrap.feature_flags[`frontend.addon.${addon.id}`],
  );
}

export function addonApplicationRoutes(
  bootstrap: FrontendBootstrap,
): string[] {
  return bootstrap.addons
    .filter((addon) => addonApplicationEnabled(addon, bootstrap))
    .flatMap((addon) => addon.legacy_routes)
    .map(normalizedPath)
    .filter((route, index, routes) => routes.indexOf(route) === index)
    .sort((left, right) => right.length - left.length);
}

export function findAddonApplication(
  bootstrap: FrontendBootstrap,
  rawPath: string,
): AddonApplicationMatch | undefined {
  const pathname = normalizedPath(rawPath);
  const matches = bootstrap.addons
    .filter((addon) => addonApplicationEnabled(addon, bootstrap))
    .flatMap((addon) =>
      addon.legacy_routes.map((route) => ({
        addon,
        basePath: normalizedPath(route),
      })),
    )
    .filter((match) => routeMatches(pathname, match.basePath))
    .sort((left, right) => right.basePath.length - left.basePath.length);
  return matches[0];
}

function abortedError(): Error {
  const error = new Error("Addon application mounting was cancelled.");
  error.name = "AbortError";
  return error;
}

export async function mountAddonApplication(
  match: AddonApplicationMatch,
  target: Element,
  bootstrap: FrontendBootstrap,
  currentPath: string,
  options: AddonApplicationMountOptions,
): Promise<MountedFrontend> {
  const { addon, basePath } = match;
  if (!addonApplicationEnabled(addon, bootstrap)) {
    throw new Error(`Addon application '${addon.id}' is not enabled.`);
  }
  if (!addon.module_url || !addon.frontend_api_range) {
    throw new Error(`Addon application '${addon.id}' has no module entrypoint.`);
  }

  const document = options.document ?? target.ownerDocument ?? globalThis.document;
  const applicationOrigin =
    options.applicationOrigin ?? document?.location?.origin;
  if (!document?.head || !applicationOrigin) {
    throw new TypeError("An addon application requires a browser document.");
  }

  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener("abort", abort, { once: true });
  if (options.signal?.aborted) {
    controller.abort();
  }

  target.setAttribute(ADDON_TARGET_ATTRIBUTE, addon.id);
  target.setAttribute(LEGACY_ADDON_TARGET_ATTRIBUTE, addon.id);
  target.setAttribute("data-addon-id", addon.id);
  target.replaceChildren();

  let mounted: MountedFrontend | undefined;
  let disposed = false;
  const dispose = async (): Promise<void> => {
    if (disposed) return;
    disposed = true;
    controller.abort();
    options.signal?.removeEventListener("abort", abort);
    try {
      const activeMount = mounted;
      mounted = undefined;
      await activeMount?.unmount();
    } finally {
      target.replaceChildren();
      target.removeAttribute("data-addon-id");
      target.removeAttribute(ADDON_TARGET_ATTRIBUTE);
      target.removeAttribute(LEGACY_ADDON_TARGET_ATTRIBUTE);
    }
  };

  try {
    const descriptor = {
      id: addon.id,
      module_url: addon.module_url,
      frontend_api_range: addon.frontend_api_range,
    };
    const [module] = await Promise.all([
      loadVersionedFrontendModule<AddonMountContext>(
        descriptor,
        applicationOrigin,
        options.importModule,
      ) as Promise<AddonApplicationModule>,
      (options.loadStylesheets ?? loadFrontendStylesheets)(
        addon.css_urls,
        applicationOrigin,
        document,
      ),
    ]);
    if (controller.signal.aborted) {
      throw abortedError();
    }

    const api = createAddonApiClient(
      applicationOrigin,
      addon.id,
      options.fetchImplementation,
    );
    const context: AddonMountContext = {
      addonId: addon.id,
      api,
      apiVersion: FRONTEND_API_VERSION,
      basePath,
      currentPath,
      featureFlags: bootstrap.feature_flags,
      navigate: options.navigate,
      signal: controller.signal,
      uiComponents: createUiComponentService(bootstrap, applicationOrigin),
    };
    mounted = await module.mount(target, context);
    if (!mounted || typeof mounted.unmount !== "function") {
      throw new TypeError(
        `Frontend module '${addon.id}' did not return an unmount handle.`,
      );
    }
    if (controller.signal.aborted) {
      await dispose();
      throw abortedError();
    }
    return {
      unmount: dispose,
      updateCurrentPath(nextPath) {
        return mounted?.updateCurrentPath?.(nextPath);
      },
    };
  } catch (error) {
    await dispose();
    throw error;
  }
}
