import type { FrontendApiClient } from "./contracts.ts";

type FrontendRequestResolver = (path: string) => URL;

const unsafeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function browserCsrfToken(): string | null {
  if (typeof document === "undefined") {
    return null;
  }
  return document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content ?? null;
}

function applicationOrigin(rawOrigin: string): URL {
  const origin = new URL(rawOrigin);
  if (!["http:", "https:"].includes(origin.protocol)) {
    throw new TypeError("The application origin must use HTTP or HTTPS.");
  }
  return origin;
}

function createJsonApiClient(
  resolveRequest: FrontendRequestResolver,
  fetchImplementation: typeof fetch,
): FrontendApiClient {
  return {
    async request<T>(path: string, init: RequestInit = {}): Promise<T> {
      const url = resolveRequest(path);
      const headers = new Headers(init.headers);
      headers.set("Accept", "application/json");
      const method = String(init.method ?? "GET").toUpperCase();
      const csrfToken = browserCsrfToken();
      if (unsafeMethods.has(method) && csrfToken && !headers.has("X-CSRF-Token")) {
        headers.set("X-CSRF-Token", csrfToken);
      }
      const response = await fetchImplementation(url, {
        ...init,
        credentials: "same-origin",
        headers,
      });
      if (!response.ok) {
        let message = `Frontend API request failed with HTTP ${response.status}.`;
        let errorPayload: unknown = null;
        const contentType = response.headers.get("content-type") ?? "";
        if (contentType.includes("application/json")) {
          errorPayload = await response.json().catch(() => null);
          const payload = errorPayload as
            | { error?: unknown; message?: unknown }
            | null;
          const detail = payload?.error ?? payload?.message;
          if (typeof detail === "string" && detail.trim()) {
            message = detail;
          }
        }
        const error = new Error(message);
        Object.assign(error, {
          status: response.status,
          payload: errorPayload,
        });
        throw error;
      }
      if (response.status === 204) {
        return undefined as T;
      }
      return (await response.json()) as T;
    },
  };
}

export function createFrontendApiClient(
  rawApplicationOrigin: string,
  fetchImplementation: typeof fetch = fetch,
): FrontendApiClient {
  const origin = applicationOrigin(rawApplicationOrigin);

  return createJsonApiClient(
    (path) => {
      const url = new URL(path, origin);
      if (url.origin !== origin.origin || !url.pathname.startsWith("/api/")) {
        throw new TypeError(
          "Frontend API requests must target a same-origin /api/ path.",
        );
      }
      return url;
    },
    fetchImplementation,
  );
}

export function createAddonApiClient(
  rawApplicationOrigin: string,
  addonId: string,
  fetchImplementation: typeof fetch = fetch,
): FrontendApiClient {
  const origin = applicationOrigin(rawApplicationOrigin);
  if (!/^[A-Za-z0-9_.-]+$/.test(addonId)) {
    throw new TypeError("Addon API clients require a valid addon identifier.");
  }
  const basePath = `/addons/${addonId}`;

  return createJsonApiClient(
    (path) => {
      const requested = new URL(path, origin);
      if (
        requested.origin !== origin.origin ||
        !requested.pathname.startsWith("/api/")
      ) {
        throw new TypeError(
          "Addon API requests must use a same-origin /api/ path.",
        );
      }
      return new URL(
        `${basePath}${requested.pathname}${requested.search}`,
        origin,
      );
    },
    fetchImplementation,
  );
}
