import type { CoreRouteLoader } from "./core-route-contract.ts";

export const routeLoaders: Record<string, CoreRouteLoader> = {
  "/": () => import("../routes/DocumentsHomePage.svelte"),
  "/settings": () => import("../routes/SettingsPage.svelte"),
};
