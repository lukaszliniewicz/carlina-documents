import {
  addonApplicationRoutes,
  type FrontendApplication,
  type FrontendBootstrap,
} from "@carlina/sdk";

export function frontendApplication(
  bootstrap: FrontendBootstrap,
): FrontendApplication {
  return bootstrap.application ?? {
    profile: "workbench",
    label: "Workbench",
    home_url: "/",
  };
}

export function frontendCoreRoutes(bootstrap: FrontendBootstrap): string[] {
  if (frontendApplication(bootstrap).profile === "documents") {
    return ["/", "/settings"];
  }
  return [
    "/",
    "/workspaces",
    "/automations",
    "/runs",
    "/approvals",
    ...(bootstrap.user.is_admin ? ["/runtime/processes"] : []),
    "/knowledge",
    "/logs",
    "/settings",
  ];
}

export function frontendClientPaths(bootstrap: FrontendBootstrap): string[] {
  const coreRoutes = frontendCoreRoutes(bootstrap);
  const documents = frontendApplication(bootstrap).profile === "documents";
  return [...new Set([
    ...(!documents ? [
      "/automations",
      "/approvals",
      ...(bootstrap.user.is_admin ? ["/runtime/processes"] : []),
    ] : []),
    ...bootstrap.navigation
      .filter((item) => item.client_route)
      .map((item) => item.href)
      .filter((href) => {
        const path = new URL(href, "https://carlina.local").pathname
          .replace(/\/+$/, "") || "/";
        return coreRoutes.includes(path);
      }),
    ...addonApplicationRoutes(bootstrap),
  ])];
}

export function frontendChatEnabled(bootstrap: FrontendBootstrap): boolean {
  return frontendApplication(bootstrap).profile === "workbench"
    && bootstrap.feature_flags["frontend.chat"] !== false;
}

export function frontendSettingsRoute(
  bootstrap: FrontendBootstrap,
  route: string,
): string | null {
  if (frontendApplication(bootstrap).profile !== "documents") return null;
  const path = new URL(route, "https://carlina.local").pathname;
  return ["guidance", "policy", "administration"].some(
    (section) => path === `/settings/${section}`
      || path.startsWith(`/settings/${section}/`),
  ) ? "/settings/models" : null;
}
