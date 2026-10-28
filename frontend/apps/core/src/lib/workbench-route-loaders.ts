import type { CoreRouteLoader } from "./core-route-contract.ts";

export const routeLoaders: Record<string, CoreRouteLoader> = {
  "/": () => import("../routes/ChatPage.svelte"),
  "/workspaces": () => import("../routes/WorkspacesPage.svelte"),
  "/automations": () => import("../routes/AutomationsPage.svelte"),
  "/runs": () => import("../routes/RunStudioPage.svelte"),
  "/approvals": () => import("../routes/RuntimeOperationsPage.svelte"),
  "/runtime/processes": () => import("../routes/RuntimeOperationsPage.svelte"),
  "/knowledge": () => import("../routes/KnowledgePage.svelte"),
  "/logs": () => import("../routes/LogsPage.svelte"),
  "/settings": () => import("../routes/SettingsPage.svelte"),
};
