import {
  FRONTEND_API_VERSION,
  SUPPORTED_FRONTEND_API_MAJORS,
  type FrontendModuleDescriptor,
  type FrontendMountContext,
  type MountedFrontend,
  type VersionedFrontendModule,
} from "./contracts.ts";

const ALLOWED_MODULE_PREFIXES = ["/static/", "/addons/"] as const;

export type ModuleImporter = (
  url: string,
) => Promise<Record<string, unknown>>;

const defaultModuleImporter: ModuleImporter = (url) =>
  import(/* @vite-ignore */ url) as Promise<Record<string, unknown>>;

export function frontendApiMajor(rawVersion: string): number {
  const normalized = rawVersion.trim().replace(/^\^/, "");
  const major = Number.parseInt(normalized.split(".", 1)[0] ?? "", 10);
  if (!Number.isInteger(major) || major < 0) {
    throw new TypeError(`Invalid frontend API version: ${rawVersion}`);
  }
  return major;
}

export function assertFrontendApiCompatibility(
  rawVersion: string,
  supportedMajors: readonly number[] = SUPPORTED_FRONTEND_API_MAJORS,
): void {
  const major = frontendApiMajor(rawVersion);
  if (!supportedMajors.includes(major)) {
    throw new TypeError(
      `Unsupported frontend API major ${major}; supported: ${supportedMajors.join(", ")}.`,
    );
  }
}

export function resolveSameOriginModuleUrl(
  rawUrl: string,
  applicationOrigin: string,
): string {
  const base = new URL(applicationOrigin);
  if (!["http:", "https:"].includes(base.protocol)) {
    throw new TypeError("The application origin must use HTTP or HTTPS.");
  }

  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(rawUrl.split(/[?#]/, 1)[0] ?? "");
  } catch {
    throw new TypeError("Frontend module URL contains invalid encoding.");
  }
  if (decodedPath.replaceAll("\\", "/").split("/").includes("..")) {
    throw new TypeError("Frontend module URLs cannot contain path traversal.");
  }

  const candidate = new URL(rawUrl, base);
  if (candidate.origin !== base.origin) {
    throw new TypeError(
      "Frontend modules must be loaded from the application origin.",
    );
  }
  if (candidate.username || candidate.password) {
    throw new TypeError("Frontend module URLs cannot contain credentials.");
  }
  if (
    !ALLOWED_MODULE_PREFIXES.some((prefix) =>
      candidate.pathname.startsWith(prefix),
    )
  ) {
    throw new TypeError(
      "Frontend module URLs must use a platform static or addon path.",
    );
  }

  return candidate.href;
}

function isVersionedModule(
  value: Record<string, unknown>,
): boolean {
  return (
    typeof value.apiVersion === "string" &&
    typeof value.mount === "function"
  );
}

export async function loadVersionedFrontendModule<
  Context extends FrontendMountContext = FrontendMountContext,
>(
  descriptor: FrontendModuleDescriptor,
  applicationOrigin: string,
  importModule: ModuleImporter = defaultModuleImporter,
): Promise<VersionedFrontendModule<Context>> {
  assertFrontendApiCompatibility(descriptor.frontend_api_range);
  const moduleUrl = resolveSameOriginModuleUrl(
    descriptor.module_url,
    applicationOrigin,
  );
  const loaded = await importModule(moduleUrl);
  if (!isVersionedModule(loaded)) {
    throw new TypeError(
      `Frontend module '${descriptor.id}' does not export apiVersion and mount.`,
    );
  }
  const versioned = loaded as unknown as VersionedFrontendModule<Context>;
  assertFrontendApiCompatibility(versioned.apiVersion);
  if (
    frontendApiMajor(versioned.apiVersion) !==
    frontendApiMajor(FRONTEND_API_VERSION)
  ) {
    throw new TypeError(
      `Frontend module '${descriptor.id}' targets an incompatible API version.`,
    );
  }
  return versioned;
}

export async function mountVersionedFrontendModule<
  Context extends FrontendMountContext,
>(
  descriptor: FrontendModuleDescriptor,
  target: Element,
  context: Context,
  applicationOrigin: string,
  importModule: ModuleImporter = defaultModuleImporter,
): Promise<MountedFrontend> {
  if (!(target instanceof Element)) {
    throw new TypeError("A frontend module requires a DOM Element target.");
  }
  const loaded = await loadVersionedFrontendModule<Context>(
    descriptor,
    applicationOrigin,
    importModule,
  );
  const mounted = await loaded.mount(target, context);
  if (!mounted || typeof mounted.unmount !== "function") {
    throw new TypeError(
      `Frontend module '${descriptor.id}' did not return an unmount handle.`,
    );
  }
  return mounted;
}
