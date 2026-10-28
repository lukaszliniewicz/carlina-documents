import type { Component } from "svelte";
import type { FrontendApiClient, FrontendBootstrap } from "@carlina/sdk";

export interface CoreRoutePageProps {
  api: FrontendApiClient;
  bootstrap: FrontendBootstrap;
  route: string;
  navigate: (
    href: string,
    clientRoute?: boolean,
    options?: { replace?: boolean },
  ) => "client" | "document";
  refreshBootstrap: () => Promise<FrontendBootstrap>;
}

export type CoreRouteLoader = () => Promise<{
  default: Component<CoreRoutePageProps>;
}>;
